#!/usr/bin/env python3
"""Claude Code Telegram Bridge — control Claude Code from your phone."""

import os
import json
import asyncio
import logging
import shutil
from pathlib import Path
from datetime import datetime

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_DIR = Path(__file__).resolve().parent

# Load .env (simple key=value, no dependency)
_env_file = BOT_DIR / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WORK_DIR = os.environ.get("CLAUDE_WORK_DIR", str(Path.home()))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", shutil.which("claude") or "claude")
OWNER_FILE = BOT_DIR / "owner.json"
SESSION_FILE = BOT_DIR / "sessions.json"
SETTINGS_FILE = BOT_DIR / "settings.json"
RECENTS_FILE = BOT_DIR / "recents.json"
COMMAND_TIMEOUT = int(os.environ.get("COMMAND_TIMEOUT", "300"))  # seconds
MAX_MSG_LEN = 4096

MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-5-20250929",
    "haiku": "claude-haiku-4-5-20251001",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)
logger = logging.getLogger("claude-tg")

# ---------------------------------------------------------------------------
# Build a clean env for the Claude subprocess (strip nesting markers)
# ---------------------------------------------------------------------------

def _claude_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    return env

# ---------------------------------------------------------------------------
# Skill discovery — scan installed Claude Code plugins for user-invocable skills
# ---------------------------------------------------------------------------

PLUGINS_DIR = Path.home() / ".claude" / "plugins"


def discover_skills() -> list[dict]:
    """Scan installed plugins for user-invocable skills.

    Returns a list of dicts: {"name": str, "plugin": str, "slash": str}
    """
    manifest = PLUGINS_DIR / "installed_plugins.json"
    if not manifest.exists():
        logger.warning("No installed_plugins.json found")
        return []

    installed = json.loads(manifest.read_text())
    skills: list[dict] = []
    seen: set[str] = set()

    for plugin_key, versions in installed.get("plugins", {}).items():
        if not versions:
            continue
        install_path = Path(versions[-1]["installPath"])
        plugin_name = plugin_key.split("@")[0]

        # Pattern 1: skills/*/SKILL.md (superpowers, claude-md-management)
        for skill_md in sorted(install_path.rglob("skills/*/SKILL.md")):
            skill_name = skill_md.parent.name
            # Skip if nested deeper (e.g. skills/notion/subskill/SKILL.md handled below)
            if skill_md.parent.parent.name != "skills":
                continue
            if skill_name in seen:
                continue
            seen.add(skill_name)
            skills.append({
                "name": skill_name,
                "plugin": plugin_name,
                "slash": f"/{skill_name}",
            })

        # Pattern 2: commands/*.md and commands/*/*.md (code-review, Notion, pr-review-toolkit)
        commands_dir = install_path / "commands"
        if commands_dir.is_dir():
            for cmd_md in sorted(commands_dir.rglob("*.md")):
                # Derive skill name from path: commands/foo.md -> foo, commands/tasks/build.md -> tasks:build
                rel = cmd_md.relative_to(commands_dir)
                parts = list(rel.with_suffix("").parts)
                skill_name = ":".join(parts)  # e.g. "find", "tasks:build"
                if skill_name in seen:
                    continue
                seen.add(skill_name)
                skills.append({
                    "name": skill_name,
                    "plugin": plugin_name,
                    "slash": f"/{skill_name}",
                })

        # Pattern 3: skills/*/*/SKILL.md (Notion deep skills like skills/notion/knowledge-capture/)
        for skill_md in sorted(install_path.rglob("skills/*/*/SKILL.md")):
            skill_name = skill_md.parent.name
            if skill_name in seen:
                continue
            seen.add(skill_name)
            skills.append({
                "name": skill_name,
                "plugin": plugin_name,
                "slash": f"/{skill_name}",
            })

    skills.sort(key=lambda s: s["name"])
    logger.info("Discovered %d skills from %d plugins", len(skills), len(installed.get("plugins", {})))
    return skills


_skills: list[dict] = discover_skills()


# ---------------------------------------------------------------------------
# Owner persistence (first /start wins)
# ---------------------------------------------------------------------------

_owner_id: int | None = None


def _load_owner() -> int | None:
    global _owner_id
    if OWNER_FILE.exists():
        _owner_id = json.loads(OWNER_FILE.read_text()).get("owner_id")
    return _owner_id


def _save_owner(uid: int) -> None:
    global _owner_id
    _owner_id = uid
    OWNER_FILE.write_text(json.dumps({"owner_id": uid}))
    logger.info("Owner set to %s", uid)


_load_owner()

# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

_settings: dict[str, str] = {}


def _load_settings() -> None:
    global _settings
    if SETTINGS_FILE.exists():
        _settings = json.loads(SETTINGS_FILE.read_text())
    logger.info("Loaded settings: %s", _settings)


def _save_settings() -> None:
    SETTINGS_FILE.write_text(json.dumps(_settings, indent=2))


_load_settings()

# ---------------------------------------------------------------------------
# Recents persistence (per-user, max 5, most-recent-first)
# ---------------------------------------------------------------------------

_recents: dict[int, list[str]] = {}


def _load_recents() -> None:
    global _recents
    if RECENTS_FILE.exists():
        raw = json.loads(RECENTS_FILE.read_text())
        _recents = {int(k): v for k, v in raw.items()}
    logger.info("Loaded recents for %d user(s)", len(_recents))


def _save_recents() -> None:
    RECENTS_FILE.write_text(json.dumps({str(k): v for k, v in _recents.items()}, indent=2))


def _record_recent(chat_id: int, name: str) -> None:
    lst = _recents.get(chat_id, [])
    if name in lst:
        lst.remove(name)
    lst.insert(0, name)
    _recents[chat_id] = lst[:5]
    _save_recents()


_load_recents()

# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


class Session:
    def __init__(
        self,
        session_id: str | None = None,
        created_at: str | None = None,
        message_count: int = 0,
    ):
        self.session_id = session_id
        self.created_at = created_at
        self.message_count = message_count
        self.busy = False
        self.pending_skill: str | None = None  # ephemeral, not persisted

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "message_count": self.message_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            session_id=d.get("session_id"),
            created_at=d.get("created_at"),
            message_count=d.get("message_count", 0),
        )


_sessions: dict[int, Session] = {}


def _load_sessions() -> None:
    if SESSION_FILE.exists():
        raw = json.loads(SESSION_FILE.read_text())
        for cid, data in raw.items():
            _sessions[int(cid)] = Session.from_dict(data)
    logger.info("Loaded %d session(s)", len(_sessions))


def _save_sessions() -> None:
    SESSION_FILE.write_text(
        json.dumps({str(k): v.to_dict() for k, v in _sessions.items()}, indent=2)
    )


_load_sessions()


def _get_session(chat_id: int) -> Session:
    if chat_id not in _sessions:
        _sessions[chat_id] = Session()
    return _sessions[chat_id]


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------


async def run_claude(
    prompt: str,
    session_id: str | None = None,
    timeout: int = COMMAND_TIMEOUT,
) -> dict:
    """Run ``claude -p`` and return parsed JSON result.

    Uses create_subprocess_exec (arg-list form, no shell) for safety.
    """

    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]

    # Inject flags from settings
    if model := _settings.get("model"):
        cmd += ["--model", model]
    if _settings.get("skip_permissions") == "1":
        cmd.append("--dangerously-skip-permissions")

    logger.info("Running: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=WORK_DIR,
        env=_claude_env(),
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {
            "is_error": True,
            "result": f"Timed out after {timeout}s",
            "session_id": session_id,
        }

    raw = stdout.decode()

    if proc.returncode != 0:
        err = stderr.decode().strip() or raw.strip() or f"Exit code {proc.returncode}"
        return {"is_error": True, "result": err, "session_id": session_id}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"result": raw.strip(), "session_id": session_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_message(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    """Split text into Telegram-friendly chunks."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 4:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 4:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 4:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def _format_result(data: dict) -> str:
    if data.get("is_error"):
        return f"Error:\n{data.get('result', 'unknown error')}"

    result = data.get("result", "")
    if not result:
        return "(done — no output)"

    parts = [result]
    meta: list[str] = []
    if data.get("cost_usd"):
        meta.append(f"${data['cost_usd']:.4f}")
    if data.get("num_turns"):
        meta.append(f"{data['num_turns']} turn(s)")
    if data.get("duration_ms"):
        secs = data["duration_ms"] / 1000
        meta.append(f"{secs:.1f}s")
    if meta:
        parts.append(f"\n[{' | '.join(meta)}]")

    return "".join(parts)


async def _keep_typing(chat, stop: asyncio.Event) -> None:
    """Send typing action every 4s until stopped."""
    while not stop.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _kb_main_menu(chat_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    # Recents row
    recents = _recents.get(chat_id, [])
    if recents:
        rows.append([_btn(f"⚡ {r}", f"sk:{r}") for r in recents[:3]])

    # Category buttons
    rows.append([_btn("🛠 Skills", "cat:skills"), _btn("📂 Git", "cat:git")])
    rows.append([_btn("⚙ Settings", "cat:settings"), _btn("📋 Session", "cat:session")])
    return InlineKeyboardMarkup(rows)


def _kb_skills() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for sk in _skills:
        pair.append(_btn(sk["name"], f"sk:{sk['name']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([_btn("« Back", "back")])
    return InlineKeyboardMarkup(rows)


def _kb_git() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("status", "git:status"), _btn("diff", "git:diff"), _btn("log", "git:log")],
        [_btn("commit", "git:commit"), _btn("branch", "git:branch"), _btn("stash", "git:stash")],
        [_btn("undo", "git:undo"), _btn("pr", "git:pr")],
        [_btn("« Back", "back")],
    ])


def _kb_settings() -> InlineKeyboardMarkup:
    model = _settings.get("model", "default")
    # Show short alias if possible
    model_label = model
    for alias, full in MODEL_ALIASES.items():
        if full == model:
            model_label = alias
            break
    sudo = "ON" if _settings.get("skip_permissions") == "1" else "OFF"
    return InlineKeyboardMarkup([
        [_btn(f"Model: {model_label}", "set:model")],
        [_btn(f"Sudo: {sudo}", "set:sudo")],
        [_btn(f"Work Dir: {WORK_DIR}", "noop")],
        [_btn("« Back", "back")],
    ])


def _kb_model_picker() -> InlineKeyboardMarkup:
    current = _settings.get("model", "")
    rows = []
    for alias, full in MODEL_ALIASES.items():
        check = " ✓" if full == current else ""
        rows.append([_btn(f"{alias}{check}", f"set:model:{alias}")])
    rows.append([_btn("default" + (" ✓" if not current else ""), "set:model:default")])
    rows.append([_btn("« Back", "cat:settings")])
    return InlineKeyboardMarkup(rows)


def _kb_session() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("📊 Info", "ses:info"), _btn("🆕 New", "ses:new")],
        [_btn("📦 Compact", "ses:compact"), _btn("🗑 Clear", "ses:clear")],
        [_btn("« Back", "back")],
    ])


def _kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("Cancel", "cancel")]])


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------


def _auth(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if _owner_id is None:
            _save_owner(uid)
        elif uid != _owner_id:
            await update.message.reply_text("Unauthorized.")
            return
        return await fn(update, ctx)

    return wrapper


def _auth_callback(fn):
    """Auth decorator for callback query handlers."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if _owner_id is not None and uid != _owner_id:
            await update.callback_query.answer("Unauthorized.", show_alert=True)
            return
        return await fn(update, ctx)

    return wrapper


# ---------------------------------------------------------------------------
# Core relay — send prompt to Claude Code, reply with result
# ---------------------------------------------------------------------------


async def _relay(update: Update, prompt: str, *, new_session: bool = False) -> None:
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if session.busy:
        await update.message.reply_text(
            "Claude Code is still working on the previous request. Please wait."
        )
        return

    session.busy = True
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        _keep_typing(update.effective_chat, stop_typing)
    )

    try:
        sid = None if new_session else session.session_id
        result = await run_claude(prompt, session_id=sid)

        # If --resume failed, retry without it (stale session)
        if result.get("is_error") and sid:
            logger.warning("Session %s failed, retrying fresh", sid)
            result = await run_claude(prompt, session_id=None)

        # Update session tracking
        new_sid = result.get("session_id")
        if new_sid:
            if not session.session_id or new_session:
                session.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            session.session_id = new_sid
            session.message_count += 1
            _save_sessions()

        response = _format_result(result)
        for chunk in _split_message(response):
            try:
                await update.message.reply_text(chunk, parse_mode="Markdown")
            except Exception:
                # Fallback: send as plain text if markdown parsing fails
                await update.message.reply_text(chunk)

    finally:
        session.busy = False
        stop_typing.set()
        typing_task.cancel()


# ---------------------------------------------------------------------------
# Callback query handler (button taps)
# ---------------------------------------------------------------------------

MENU_TEXT = "Claude Code Bridge — tap a button or type a message."


@_auth_callback
async def handle_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    # Clear any pending skill when navigating menus
    if data not in ("cancel",) and not data.startswith("sk:"):
        session.pending_skill = None

    # --- Navigation ---
    if data == "menu" or data == "back":
        await query.edit_message_text(MENU_TEXT, reply_markup=_kb_main_menu(chat_id))
        return

    if data == "noop":
        return

    if data == "cancel":
        session.pending_skill = None
        await query.edit_message_text(MENU_TEXT, reply_markup=_kb_main_menu(chat_id))
        return

    # --- Categories ---
    if data == "cat:skills":
        await query.edit_message_text("🛠 *Skills*\nTap to activate, then type your message.", parse_mode="Markdown", reply_markup=_kb_skills())
        return

    if data == "cat:git":
        await query.edit_message_text("📂 *Git*", parse_mode="Markdown", reply_markup=_kb_git())
        return

    if data == "cat:settings":
        await query.edit_message_text("⚙ *Settings*", parse_mode="Markdown", reply_markup=_kb_settings())
        return

    if data == "cat:session":
        await query.edit_message_text("📋 *Session*", parse_mode="Markdown", reply_markup=_kb_session())
        return

    # --- Skill activation ---
    if data.startswith("sk:"):
        skill_name = data[3:]
        session.pending_skill = skill_name
        await query.edit_message_text(
            f"🛠 *{skill_name}*\nType your message (it will be sent as `/{skill_name} <your text>`).",
            parse_mode="Markdown",
            reply_markup=_kb_cancel(),
        )
        return

    # --- Git commands ---
    if data.startswith("git:"):
        action = data[4:]
        # Immediate commands (no input needed)
        immediate = {
            "status": "Run `git status` and show the output concisely.",
            "diff": "Run `git diff` and show the output. If large, summarize key changes.",
            "log": "Run `git log --oneline -n 10` and show the output.",
            "undo": "Run `git reset --soft HEAD~1` and show result.",
        }
        if action in immediate:
            await query.edit_message_text(f"📂 Running git {action}...")
            _record_recent(chat_id, f"git:{action}")
            await _relay_from_callback(update, immediate[action])
            return

        # Commands needing input
        prompts = {
            "commit": ("Commit message (or leave blank):", "git:commit"),
            "branch": ("Branch name (or blank to list all):", "git:branch"),
            "stash": ("Stash operation (list/push/pop/drop):", "git:stash"),
            "pr": ("PR description (or blank for auto):", "git:pr"),
        }
        if action in prompts:
            label, tag = prompts[action]
            session.pending_skill = tag  # reuse pending_skill for git too
            await query.edit_message_text(
                f"📂 *git {action}*\n{label}",
                parse_mode="Markdown",
                reply_markup=_kb_cancel(),
            )
            return

    # --- Session commands ---
    if data.startswith("ses:"):
        action = data[4:]
        if action == "info":
            s = _get_session(chat_id)
            if not s.session_id:
                await query.edit_message_text("No active session. Send a message to start one.", reply_markup=_kb_main_menu(chat_id))
                return
            model = _settings.get("model", "default")
            sudo = "enabled" if _settings.get("skip_permissions") == "1" else "disabled"
            await query.edit_message_text(
                f"📋 *Session Info*\n"
                f"ID: `{s.session_id}`\n"
                f"Started: {s.created_at}\n"
                f"Messages: {s.message_count}\n"
                f"Model: {model}\n"
                f"Sudo: {sudo}",
                parse_mode="Markdown",
                reply_markup=_kb_main_menu(chat_id),
            )
            return

        if action == "new":
            old = _get_session(chat_id).session_id
            _sessions[chat_id] = Session()
            _save_sessions()
            msg = "🆕 New session started."
            if old:
                msg += f"\nPrevious: `{old[:16]}...`"
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=_kb_main_menu(chat_id))
            return

        if action == "compact":
            await query.edit_message_text("📦 Compacting session...")
            s = _get_session(chat_id)
            if not s.session_id:
                await query.edit_message_text("No active session to compact.", reply_markup=_kb_main_menu(chat_id))
                return
            s.busy = True
            try:
                summary = await run_claude(
                    "Provide a concise summary of our entire conversation so far: "
                    "key decisions, files modified, current state, and pending work.",
                    session_id=s.session_id,
                )
                summary_text = summary.get("result", "")
                if not summary_text:
                    await query.edit_message_text("Failed to generate summary.", reply_markup=_kb_main_menu(chat_id))
                    return
                fresh = await run_claude(
                    f"CONTEXT FROM PREVIOUS SESSION:\n\n{summary_text}\n\n"
                    "Acknowledged. I have the context. Ready to continue.",
                )
                old_count = s.message_count
                new_s = Session(
                    session_id=fresh.get("session_id"),
                    created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                    message_count=1,
                )
                _sessions[chat_id] = new_s
                _save_sessions()
                await query.edit_message_text(
                    f"📦 Session compacted ({old_count} msgs → fresh start).",
                    reply_markup=_kb_main_menu(chat_id),
                )
            finally:
                s.busy = False
            return

        if action == "clear":
            _sessions.pop(chat_id, None)
            _save_sessions()
            await query.edit_message_text("🗑 Session cleared.", reply_markup=_kb_main_menu(chat_id))
            return

    # --- Settings ---
    if data == "set:model":
        await query.edit_message_text("⚙ *Select model:*", parse_mode="Markdown", reply_markup=_kb_model_picker())
        return

    if data.startswith("set:model:"):
        choice = data[len("set:model:"):]
        if choice == "default":
            _settings.pop("model", None)
        else:
            _settings["model"] = MODEL_ALIASES.get(choice, choice)
        _save_settings()
        await query.edit_message_text(
            f"⚙ Model set to *{choice}*",
            parse_mode="Markdown",
            reply_markup=_kb_settings(),
        )
        return

    if data == "set:sudo":
        if _settings.get("skip_permissions") == "1":
            _settings["skip_permissions"] = "0"
        else:
            _settings["skip_permissions"] = "1"
        _save_settings()
        state = "ON" if _settings.get("skip_permissions") == "1" else "OFF"
        await query.edit_message_text(
            f"⚙ Sudo is now *{state}*",
            parse_mode="Markdown",
            reply_markup=_kb_settings(),
        )
        return


async def _relay_from_callback(update: Update, prompt: str, *, new_session: bool = False) -> None:
    """Like _relay but works from a callback query (no update.message)."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if session.busy:
        await update.effective_chat.send_message("Claude Code is still working on the previous request. Please wait.")
        return

    session.busy = True
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(_keep_typing(update.effective_chat, stop_typing))

    try:
        sid = None if new_session else session.session_id
        result = await run_claude(prompt, session_id=sid)

        if result.get("is_error") and sid:
            logger.warning("Session %s failed, retrying fresh", sid)
            result = await run_claude(prompt, session_id=None)

        new_sid = result.get("session_id")
        if new_sid:
            if not session.session_id or new_session:
                session.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            session.session_id = new_sid
            session.message_count += 1
            _save_sessions()

        response = _format_result(result)
        for chunk in _split_message(response):
            try:
                await update.effective_chat.send_message(chunk, parse_mode="Markdown")
            except Exception:
                await update.effective_chat.send_message(chunk)

    finally:
        session.busy = False
        stop_typing.set()
        typing_task.cancel()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "*Claude Code Bridge*\n\n"
    "Send any message to chat with Claude Code.\n\n"
    "*Session*\n"
    "/new — Fresh session\n"
    "/session — Current session info\n"
    "/compact — Summarize & start new session\n"
    "/clear — Drop session tracking\n\n"
    "*Model & Settings*\n"
    "/model [name] — Set or show model\n"
    "/sudo [on|off] — Toggle permissions skip\n"
    "/settings — Show current settings\n\n"
    "*Git*\n"
    "/status — Git status\n"
    "/diff — Show diff\n"
    "/commit — Commit changes\n"
    "/log — Recent commits\n"
    "/branch [name] — List or switch branch\n"
    "/stash [op] — Git stash operations\n"
    "/undo — Soft reset HEAD~1\n"
    "/pr — Create a PR\n\n"
    "*Files & Misc*\n"
    "/find <pattern> — Find files\n"
    "/read <path> — Read file\n"
    "/edit <instr> — Edit via instruction\n"
    "/run <cmd> — Run a shell command\n"
    "/help — This message"
)


@_auth
async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT, reply_markup=_kb_main_menu(update.effective_chat.id))


@_auth
async def cmd_menu(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT, reply_markup=_kb_main_menu(update.effective_chat.id))


@_auth
async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


@_auth
async def cmd_new(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    old = _get_session(update.effective_chat.id).session_id
    _sessions[update.effective_chat.id] = Session()
    _save_sessions()
    msg = "New session started."
    if old:
        msg += f"\nPrevious: {old[:16]}..."
    await update.message.reply_text(msg)


@_auth
async def cmd_session(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    s = _get_session(update.effective_chat.id)
    if not s.session_id:
        await update.message.reply_text(
            "No active session. Send a message to start one."
        )
        return
    
    model = _settings.get("model", "default")
    sudo = "enabled" if _settings.get("skip_permissions") == "1" else "disabled"
    
    await update.message.reply_text(
        f"Session: {s.session_id}\n"
        f"Started: {s.created_at}\n"
        f"Messages: {s.message_count}\n"
        f"Model: {model}\n"
        f"Sudo (skip-permissions): {sudo}"
    )


@_auth
async def cmd_compact(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    s = _get_session(update.effective_chat.id)
    if not s.session_id:
        await update.message.reply_text("No active session to compact.")
        return

    s.busy = True
    stop = asyncio.Event()
    typing = asyncio.create_task(_keep_typing(update.effective_chat, stop))

    try:
        summary = await run_claude(
            "Provide a concise summary of our entire conversation so far: "
            "key decisions, files modified, current state, and pending work.",
            session_id=s.session_id,
        )
        summary_text = summary.get("result", "")
        if not summary_text:
            await update.message.reply_text("Failed to generate summary.")
            return

        fresh = await run_claude(
            f"CONTEXT FROM PREVIOUS SESSION:\n\n{summary_text}\n\n"
            "Acknowledged. I have the context. Ready to continue.",
        )

        old_count = s.message_count
        new_s = Session(
            session_id=fresh.get("session_id"),
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            message_count=1,
        )
        _sessions[update.effective_chat.id] = new_s
        _save_sessions()

        await update.message.reply_text(
            f"Session compacted ({old_count} msgs -> fresh start).\n"
            f"New session: {new_s.session_id or 'unknown'}"
        )
    finally:
        s.busy = False
        stop.set()
        typing.cancel()


@_auth
async def cmd_clear(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    _sessions.pop(update.effective_chat.id, None)
    _save_sessions()
    await update.message.reply_text("Session cleared.")


# --- Model & Settings ---


@_auth
async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        current = _settings.get("model", "default")
        aliases = "\n".join([f"- {k}: {v}" for k, v in MODEL_ALIASES.items()])
        await update.message.reply_text(
            f"Current model: {current}\n\nAliases:\n{aliases}"
        )
        return

    name = ctx.args[0].lower()
    if name in ("default", "reset"):
        _settings.pop("model", None)
    else:
        full_id = MODEL_ALIASES.get(name, ctx.args[0])
        _settings["model"] = full_id
    
    _save_settings()
    await update.message.reply_text(f"Model set to: {_settings.get('model', 'default')}")


@_auth
async def cmd_sudo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        val = ctx.args[0].lower()
        if val == "on":
            _settings["skip_permissions"] = "1"
        elif val == "off":
            _settings["skip_permissions"] = "0"
    else:
        # Toggle
        if _settings.get("skip_permissions") == "1":
            _settings["skip_permissions"] = "0"
        else:
            _settings["skip_permissions"] = "1"
    
    _save_settings()
    state = "ENABLED" if _settings.get("skip_permissions") == "1" else "DISABLED"
    await update.message.reply_text(f"Sudo (skip-permissions) is now {state}")


@_auth
async def cmd_settings(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    model = _settings.get("model", "default")
    sudo = "on" if _settings.get("skip_permissions") == "1" else "off"
    await update.message.reply_text(
        f"Model: {model}\n"
        f"Sudo: {sudo}\n"
        f"Timeout: {COMMAND_TIMEOUT}s\n"
        f"Work Dir: {WORK_DIR}"
    )


# --- Git slash commands ---


@_auth
async def cmd_commit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    extra = " ".join(ctx.args) if ctx.args else ""
    await _relay(update, f"/commit {extra}".strip())


@_auth
async def cmd_diff(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await _relay(
        update,
        "Run `git diff` and show the output. If large, summarize key changes.",
    )


@_auth
async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await _relay(update, "Run `git status` and show the output concisely.")


@_auth
async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = ctx.args[0] if ctx.args else "10"
    await _relay(update, f"Run `git log --oneline -n {n}` and show the output.")


@_auth
async def cmd_pr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    extra = " ".join(ctx.args) if ctx.args else ""
    await _relay(update, f"Create a pull request. {extra}".strip())


@_auth
async def cmd_branch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await _relay(update, "Run `git branch -a` and show the output.")
    else:
        branch = ctx.args[0]
        await _relay(update, f"Switch to (or create) branch `{branch}` and show result.")


@_auth
async def cmd_stash(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    op = ctx.args[0] if ctx.args else "list"
    await _relay(update, f"Run `git stash {op}` and show result.")


@_auth
async def cmd_undo(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    await _relay(update, "Run `git reset --soft HEAD~1` and show result.")


# --- Files & Misc ---


@_auth
async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /find <pattern>")
        return
    pattern = " ".join(ctx.args)
    await _relay(update, f"Find files matching pattern `{pattern}`.")


@_auth
async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /read <path>")
        return
    path = ctx.args[0]
    await _relay(update, f"Read the contents of `{path}`.")


@_auth
async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /edit <instruction>")
        return
    instr = " ".join(ctx.args)
    await _relay(update, instr)


@_auth
async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /run <command>")
        return
    cmd = " ".join(ctx.args)
    await _relay(
        update,
        f"Run this shell command and show the full output:\n```\n{cmd}\n```",
    )


# --- Regular messages ---


@_auth
async def handle_message(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)
    text = update.message.text

    if session.pending_skill:
        skill = session.pending_skill
        session.pending_skill = None

        # Handle git commands that were pending input
        if skill.startswith("git:"):
            action = skill[4:]
            git_prompts = {
                "commit": f"/commit {text}".strip(),
                "branch": f"Switch to (or create) branch `{text}` and show result." if text.strip() else "Run `git branch -a` and show the output.",
                "stash": f"Run `git stash {text}` and show result.",
                "pr": f"Create a pull request. {text}".strip(),
            }
            prompt = git_prompts.get(action, text)
            _record_recent(chat_id, skill)
            await _relay(update, prompt)
            return

        # Regular skill — prefix with slash command
        _record_recent(chat_id, skill)
        await _relay(update, f"/{skill} {text}")
        return

    await _relay(update, text)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("menu", "Open button menu"),
            BotCommand("new", "Fresh session"),
            BotCommand("model", "Set/show model"),
            BotCommand("sudo", "Toggle sudo"),
            BotCommand("status", "Git status"),
            BotCommand("diff", "Git diff"),
            BotCommand("commit", "Commit changes"),
            BotCommand("run", "Run shell command"),
            BotCommand("help", "Show help"),
        ]
    )
    logger.info("Bot commands registered with Telegram")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init)
        .build()
    )

    handlers = [
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("menu", cmd_menu),
        CommandHandler("new", cmd_new),
        CommandHandler("session", cmd_session),
        CommandHandler("compact", cmd_compact),
        CommandHandler("clear", cmd_clear),
        CommandHandler("model", cmd_model),
        CommandHandler("sudo", cmd_sudo),
        CommandHandler("settings", cmd_settings),
        CommandHandler("status", cmd_status),
        CommandHandler("diff", cmd_diff),
        CommandHandler("commit", cmd_commit),
        CommandHandler("log", cmd_log),
        CommandHandler("branch", cmd_branch),
        CommandHandler("stash", cmd_stash),
        CommandHandler("undo", cmd_undo),
        CommandHandler("pr", cmd_pr),
        CommandHandler("find", cmd_find),
        CommandHandler("read", cmd_read),
        CommandHandler("edit", cmd_edit),
        CommandHandler("run", cmd_run),
        CallbackQueryHandler(handle_callback),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
    ]

    for h in handlers:
        app.add_handler(h)

    logger.info("Starting bot (work_dir=%s, claude=%s)", WORK_DIR, CLAUDE_BIN)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
