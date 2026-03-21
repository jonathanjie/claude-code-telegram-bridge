#!/usr/bin/env python3
"""Claude Code Telegram Bridge — control Claude Code from your phone."""

import os
import sys
import json
import asyncio
import fcntl
import logging
import shutil
import re
import time
import html as html_module
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
COMMAND_TIMEOUT = int(os.environ.get("COMMAND_TIMEOUT", "900"))  # seconds
STALE_TIMEOUT = int(os.environ.get("STALE_TIMEOUT", "60"))  # kill subprocess if 0 CPU for this long
MAX_MSG_LEN = 4096
PHOTO_DIR = BOT_DIR / "photos"
VPS_ROUTES_FILE = BOT_DIR / "vps_routes.json"

# Claude Code session files — derive path from WORK_DIR
_CC_SESSIONS_DIR = (
    Path.home() / ".claude" / "projects" / WORK_DIR.replace("/", "-")
)

MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
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
# Instance lock — prevent dual-instance Telegram Conflict errors
# ---------------------------------------------------------------------------

_lock_fd = None


def _acquire_lock() -> None:
    """Acquire an exclusive file lock so only one bot instance can run."""
    global _lock_fd
    lock_path = BOT_DIR / "bot.lock"
    _lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another instance is already running (lock: %s)", lock_path)
        sys.exit(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
    logger.info("Acquired instance lock (pid=%d)", os.getpid())


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

    # Pattern 4: ~/.claude/commands/*.md (user-level Claude Code slash commands)
    user_commands_dir = Path.home() / ".claude" / "commands"
    if user_commands_dir.is_dir():
        for cmd_md in sorted(user_commands_dir.glob("*.md")):
            skill_name = cmd_md.stem
            if skill_name in seen:
                continue
            seen.add(skill_name)
            skills.append({
                "name": skill_name,
                "plugin": "_user-commands",
                "slash": f"/{skill_name}",
            })

    # Pattern 5: ~/.claude/skills/*/SKILL.md (user-level custom skills)
    user_skills_dir = Path.home() / ".claude" / "skills"
    if user_skills_dir.is_dir():
        for skill_md in sorted(user_skills_dir.glob("*/SKILL.md")):
            skill_name = skill_md.parent.name
            if skill_name in seen:
                continue
            seen.add(skill_name)
            skills.append({
                "name": skill_name,
                "plugin": "_custom-skills",
                "slash": f"/{skill_name}",
            })

    skills.sort(key=lambda s: s["name"])
    logger.info("Discovered %d skills from %d plugins + user commands", len(skills), len(installed.get("plugins", {})))
    return skills


_skills: list[dict] = discover_skills()

# ---------------------------------------------------------------------------
# Skill group mapping — plugin name → (emoji, display label)
# ---------------------------------------------------------------------------

SKILL_GROUPS: dict[str, tuple[str, str]] = {
    # Dev plugins
    "superpowers":          ("💥", "Superpowers"),
    "Notion":               ("📓", "Notion"),
    "atlassian":            ("🏢", "Atlassian"),
    "frontend-design":      ("🎨", "Frontend"),
    "feature-dev":          ("🔧", "Feature Dev"),
    "code-review":          ("🔍", "Code Review"),
    "pr-review-toolkit":    ("📋", "PR Review"),
    "claude-md-management": ("📝", "Project Docs"),
    "code-simplifier":      ("✨", "Simplifier"),
    "_user-commands":       ("🌐", "Browser"),
    "_custom-skills":       ("🧩", "Custom"),
    # Knowledge-work plugins
    "legal":                    ("⚖️", "Legal"),
    "finance":                  ("💵", "Finance"),
    "marketing":                ("📣", "Marketing"),
    "sales":                    ("💰", "Sales"),
    "customer-support":         ("🎧", "Support"),
    "product-management":       ("🗺", "Product"),
    "data":                     ("📊", "Data"),
    "productivity":             ("📅", "Productivity"),
    "enterprise-search":        ("🔎", "Search"),
    "bio-research":             ("🧬", "Bio Research"),
    "cowork-plugin-management": ("🔌", "Plugin Mgmt"),
}

# Knowledge-work plugins in preferred display order
_WORK_PLUGINS = [
    "legal", "finance", "marketing", "sales", "customer-support",
    "product-management", "data", "productivity", "enterprise-search",
    "bio-research", "cowork-plugin-management",
]


def _group_label(plugin: str) -> str:
    """Return 'emoji Name' for a plugin, with fallback for unknown plugins."""
    if plugin in SKILL_GROUPS:
        emoji, name = SKILL_GROUPS[plugin]
        return f"{emoji} {name}"
    return f"🔌 {plugin.replace('-', ' ').title()}"


def _group_emoji(plugin: str) -> str:
    """Return just the emoji for a plugin."""
    if plugin in SKILL_GROUPS:
        return SKILL_GROUPS[plugin][0]
    return "🔌"


def _skills_by_group() -> dict[str, list[dict]]:
    """Group discovered skills by plugin name."""
    groups: dict[str, list[dict]] = {}
    for sk in _skills:
        groups.setdefault(sk["plugin"], []).append(sk)
    return groups


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
# VPS routing — name -> work_dir mapping
# ---------------------------------------------------------------------------

_vps_routes: dict[str, dict] = {}


def _load_vps_routes() -> None:
    global _vps_routes
    if VPS_ROUTES_FILE.exists():
        _vps_routes = json.loads(VPS_ROUTES_FILE.read_text())
    logger.info("Loaded %d VPS route(s)", len(_vps_routes))


def _save_vps_routes() -> None:
    VPS_ROUTES_FILE.write_text(json.dumps(_vps_routes, indent=2))


_load_vps_routes()

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
        self.queue: list[str] = []  # ephemeral — messages queued while busy

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
# Claude Code session history — scan on-disk JSONL files
# ---------------------------------------------------------------------------


def _scan_cc_sessions(limit: int = 8, offset: int = 0) -> tuple[list[dict], int]:
    """Scan Claude Code session files and return recent sessions with metadata.

    Displays renamed session titles (slug) if available, otherwise falls back to
    the latest user message. Returns (sessions, total_count).
    """
    if not _CC_SESSIONS_DIR.is_dir():
        return [], 0

    all_files = sorted(
        _CC_SESSIONS_DIR.glob("*/*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    total = len(all_files)
    session_files = all_files[offset:offset + limit]

    results: list[dict] = []
    for f in session_files:
        sid = f.stem
        st = f.stat()
        mtime = datetime.fromtimestamp(st.st_mtime)

        # Read renamed slug (if exists) or latest user message as fallback
        prompt = ""
        latest_user_msg = ""
        try:
            with open(f) as fh:
                for line in fh:
                    entry = json.loads(line)

                    # Prefer slug (renamed session title) if present
                    if "slug" in entry:
                        slug = entry.get("slug", "").strip()
                        if slug:
                            prompt = slug
                            break

                    # Capture latest user message as fallback (keep updating to get the newest)
                    if entry.get("type") == "user":
                        msg = entry.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            # Content can be a list of blocks
                            content = " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict)
                            )
                        latest_user_msg = content.strip()
        except (json.JSONDecodeError, IOError):
            pass

        # Use slug if found, otherwise use latest user message
        if not prompt:
            prompt = latest_user_msg

        results.append({
            "session_id": sid,
            "prompt": prompt[:60] or sid[:12],
            "mtime": mtime,
            "size_kb": st.st_size / 1024,
        })

    return results, total


# ---------------------------------------------------------------------------
# Claude Code runner
# ---------------------------------------------------------------------------


def _proc_cpu_ticks(pid: int) -> int | None:
    """Read total CPU ticks (utime+stime) from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
            return int(parts[13]) + int(parts[14])
    except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
        return None


async def _cpu_watchdog(proc: asyncio.subprocess.Process, stale_limit: int) -> None:
    """Kill subprocess if it shows zero CPU activity for stale_limit seconds."""
    pid = proc.pid
    last_cpu = _proc_cpu_ticks(pid) or 0
    stale_since: float | None = None

    while proc.returncode is None:
        await asyncio.sleep(10)
        cpu = _proc_cpu_ticks(pid)
        if cpu is None:
            return  # process already gone
        if cpu == last_cpu:
            if stale_since is None:
                stale_since = time.monotonic()
            elif time.monotonic() - stale_since > stale_limit:
                logger.warning(
                    "PID %d stale for %ds (0 CPU ticks), killing", pid, stale_limit
                )
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return
        else:
            stale_since = None
            last_cpu = cpu


async def run_claude(
    prompt: str,
    session_id: str | None = None,
    timeout: int = COMMAND_TIMEOUT,
    work_dir: str | None = None,
    claude_bin: str | None = None,
) -> dict:
    """Run ``claude -p`` and return parsed JSON result.

    Uses create_subprocess_exec (arg-list form, no shell) for safety.
    *work_dir* and *claude_bin* override the global defaults when set
    (used by VPS routing).
    """
    _bin = claude_bin or CLAUDE_BIN
    _dir = work_dir or WORK_DIR

    cmd = [_bin, "-p", prompt, "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]

    # Inject flags from settings
    if model := _settings.get("model"):
        cmd += ["--model", model]
    if _settings.get("skip_permissions") == "1":
        cmd.append("--dangerously-skip-permissions")

    logger.info("Running: %s (cwd=%s)", " ".join(cmd), _dir)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=_dir,
        env=_claude_env(),
    )

    watchdog = asyncio.create_task(_cpu_watchdog(proc, STALE_TIMEOUT))

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {
            "is_error": True,
            "timed_out": True,
            "result": f"Timed out after {timeout}s",
            "session_id": session_id,
        }
    finally:
        watchdog.cancel()

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


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _extract_file_paths(text: str) -> tuple[str, list[Path]]:
    """Extract existing file paths from Claude's response for attachment.

    Returns (original_text, list_of_paths). Paths must be absolute,
    exist on disk, and be under 50MB (Telegram limit).
    """
    files: list[Path] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'`(/[^\s`]+)`|(?<!\w)(/(?:home|tmp|var|data)[^\s,)}\]]+)', text
    ):
        path_str = m.group(1) or m.group(2)
        if path_str in seen:
            continue
        seen.add(path_str)
        p = Path(path_str)
        try:
            if p.is_file() and p.stat().st_size < 50 * 1024 * 1024:
                files.append(p)
        except OSError:
            pass
    return text, files


def _md_to_tg_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML.

    Handles fenced code blocks, inline code, bold, italic, links,
    headers, and strikethrough. Falls back gracefully on edge cases.
    """
    # 1. Extract fenced code blocks (protect from further processing)
    blocks: list[str] = []

    def _stash_block(m):
        lang = m.group(1) or ""
        code = html_module.escape(m.group(2))
        if lang:
            blocks.append(f'<code class="language-{lang}">{code}</code>')
        else:
            blocks.append(code)
        return f"\x00CB{len(blocks) - 1}\x00"

    text = re.sub(r"```(\w*)\n?(.*?)```", _stash_block, text, flags=re.DOTALL)

    # 2. Extract inline code
    inlines: list[str] = []

    def _stash_inline(m):
        inlines.append(html_module.escape(m.group(1)))
        return f"\x00IC{len(inlines) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _stash_inline, text)

    # 3. Escape HTML entities in remaining text
    text = html_module.escape(text)

    # 4. Convert markdown formatting to HTML
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 5. Restore protected regions
    for i, inner in enumerate(blocks):
        text = text.replace(f"\x00CB{i}\x00", f"<pre>{inner}</pre>")
    for i, code in enumerate(inlines):
        text = text.replace(f"\x00IC{i}\x00", f"<code>{code}</code>")

    return text


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
    rows.append([_btn("🛠 Skills", "cat:skills"), _btn("💼 Work", "cat:work")])
    rows.append([_btn("📂 Git", "cat:git"), _btn("⚙ Settings", "cat:settings")])
    rows.append([_btn("📋 Session", "cat:session")])
    return InlineKeyboardMarkup(rows)


def _kb_skill_groups() -> InlineKeyboardMarkup:
    """Layer 2: show plugin groups as buttons."""
    groups = _skills_by_group()
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for plugin in sorted(groups, key=lambda p: _group_label(p)):
        emoji = _group_emoji(plugin)
        label = f"{emoji} {plugin.replace('-', ' ').title()}" if plugin not in SKILL_GROUPS else _group_label(plugin)
        pair.append(_btn(label, f"sg:{plugin}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([_btn("« Back", "back")])
    return InlineKeyboardMarkup(rows)


def _kb_skill_group(plugin: str, back_target: str = "cat:skills") -> InlineKeyboardMarkup:
    """Layer 3: show individual skills within a plugin group."""
    groups = _skills_by_group()
    skills = groups.get(plugin, [])
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for sk in skills:
        pair.append(_btn(sk["name"], f"sk:{sk['name']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([_btn("« Back", back_target)])
    return InlineKeyboardMarkup(rows)


def _kb_work_groups() -> InlineKeyboardMarkup:
    """Work skills layer 2: knowledge-work plugin categories."""
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for plugin in _WORK_PLUGINS:
        emoji, name = SKILL_GROUPS[plugin]
        pair.append(_btn(f"{emoji} {name}", f"wg:{plugin}"))
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
        [_btn("📜 History", "ses:history"), _btn("📦 Compact", "ses:compact")],
        [_btn("🗑 Clear", "ses:clear")],
        [_btn("« Back", "back")],
    ])


def _kb_session_history(
    sessions: list[dict],
    current_sid: str | None = None,
    offset: int = 0,
    total: int = 0,
    page_size: int = 5,
) -> InlineKeyboardMarkup:
    """Show recent Claude Code sessions as resumable buttons with pagination."""
    rows: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        prompt = s["prompt"]
        if len(prompt) > 40:
            prompt = prompt[:37] + "..."
        date = s["mtime"].strftime("%m/%d %H:%M")
        active = " ●" if s["session_id"] == current_sid else ""
        label = f"{prompt} ({date}){active}"
        rows.append([_btn(label, f"sr:{s['session_id']}")])

    # Pagination row
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(_btn("◀ Prev", f"sh:{offset - page_size}"))
    if offset + page_size < total:
        nav.append(_btn("Next ▶", f"sh:{offset + page_size}"))
    if nav:
        rows.append(nav)

    rows.append([_btn("« Back", "cat:session")])
    return InlineKeyboardMarkup(rows)


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


async def _run_and_send(
    chat,
    session: Session,
    prompt: str,
    *,
    new_session: bool = False,
    placeholder=None,
    vps_override: dict | None = None,
) -> None:
    """Run Claude and send the formatted response to chat.

    If *placeholder* is given, edit it with the first response chunk
    instead of sending a new message.  *vps_override* changes the
    working directory and/or claude binary for this invocation.
    """
    sid = None if new_session else session.session_id
    _wd = vps_override.get("work_dir") if vps_override else None
    _cb = vps_override.get("claude_bin") if vps_override else None
    result = await run_claude(prompt, session_id=sid, work_dir=_wd, claude_bin=_cb)

    if result.get("is_error") and sid and not result.get("timed_out"):
        logger.warning("Session %s failed, retrying fresh", sid)
        result = await run_claude(prompt, session_id=None, work_dir=_wd, claude_bin=_cb)

    new_sid = result.get("session_id")
    if new_sid:
        if not session.session_id or new_session:
            session.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        session.session_id = new_sid
        session.message_count += 1
        _save_sessions()

    response = _format_result(result)
    response, attachments = _extract_file_paths(response)
    chunks = _split_message(response)

    # Edit the placeholder with the first chunk, send the rest as new messages
    if placeholder and chunks:
        first = chunks[0]
        html_first = _md_to_tg_html(first)
        try:
            await placeholder.edit_text(html_first, parse_mode="HTML")
        except Exception:
            try:
                await placeholder.edit_text(first)
            except Exception:
                await chat.send_message(first)

        for chunk in chunks[1:]:
            html_chunk = _md_to_tg_html(chunk)
            try:
                await chat.send_message(html_chunk, parse_mode="HTML")
            except Exception:
                await chat.send_message(chunk)
    else:
        for chunk in (chunks or []):
            html_chunk = _md_to_tg_html(chunk)
            try:
                await chat.send_message(html_chunk, parse_mode="HTML")
            except Exception:
                await chat.send_message(chunk)

    # Send extracted file attachments
    for fpath in attachments:
        try:
            with open(fpath, "rb") as f:
                if fpath.suffix.lower() in _IMAGE_EXTS:
                    await chat.send_photo(photo=f, caption=fpath.name)
                else:
                    await chat.send_document(document=f, caption=fpath.name)
        except Exception as e:
            logger.warning("Failed to send file %s: %s", fpath, e)


async def _relay(
    update: Update,
    prompt: str,
    *,
    new_session: bool = False,
    vps_override: dict | None = None,
) -> None:
    """Send prompt to Claude Code, reply with result. Queues if busy."""
    chat = update.effective_chat
    session = _get_session(chat.id)

    if session.busy:
        session.queue.append(prompt)
        n = len(session.queue)
        msg = f"Queued ({n} pending)."
        if update.message:
            await update.message.reply_text(msg)
        else:
            await chat.send_message(msg)
        return

    session.busy = True

    # Send a placeholder that gets edited with the result
    # Show which VPS route is being used in the placeholder
    vps_name = ""
    if vps_override:
        for name, cfg in _vps_routes.items():
            if cfg is vps_override:
                vps_name = f" [{name}]"
                break
    placeholder = await chat.send_message(f"Working...{vps_name}")

    try:
        await _run_and_send(
            chat, session, prompt,
            new_session=new_session, placeholder=placeholder,
            vps_override=vps_override,
        )

        # Drain queue — process messages that arrived while busy
        while session.queue:
            queued = session.queue[:]
            session.queue.clear()
            combined = "\n---\n".join(queued)
            ph = await chat.send_message("Working...")
            await _run_and_send(chat, session, combined, placeholder=ph,
                                vps_override=vps_override)

    finally:
        session.busy = False


# ---------------------------------------------------------------------------
# Callback query handler (button taps)
# ---------------------------------------------------------------------------

MENU_TEXT = "Claude Code Bridge — tap a button or type a message."


async def _nav_reply(
    query,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    session: Session,
    *,
    parse_mode: str | None = None,
) -> None:
    """Send a navigation response — edit if idle, new message if busy.

    When Claude is processing, the original menu message may have been
    replaced by a status update. Sending a new message lets the user
    browse freely while waiting for the response.
    """
    if session.busy:
        await query.message.chat.send_message(
            text, reply_markup=reply_markup, parse_mode=parse_mode,
        )
    else:
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode,
        )


@_auth_callback
async def handle_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    # Allow navigation and settings even if session is busy
    # --- Navigation (all use _nav_reply for busy-aware responses) ---
    if data == "menu" or data == "back":
        await _nav_reply(query, MENU_TEXT, _kb_main_menu(chat_id), session)
        return

    if data == "noop":
        return

    if data == "cancel":
        session.pending_skill = None
        await _nav_reply(query, MENU_TEXT, _kb_main_menu(chat_id), session)
        return

    # --- Categories ---
    if data == "cat:skills":
        await _nav_reply(query, "🛠 *Skills*\nChoose a category.", _kb_skill_groups(), session, parse_mode="Markdown")
        return

    if data == "cat:work":
        await _nav_reply(query, "💼 *Work Skills*\nChoose a category.", _kb_work_groups(), session, parse_mode="Markdown")
        return

    if data.startswith("sg:"):
        plugin = data[3:]
        label = _group_label(plugin)
        await _nav_reply(
            query,
            f"{label}\nTap to activate, then type your message.",
            _kb_skill_group(plugin, back_target="cat:skills"),
            session,
        )
        return

    if data.startswith("wg:"):
        plugin = data[3:]
        label = _group_label(plugin)
        await _nav_reply(
            query,
            f"{label}\nTap to activate, then type your message.",
            _kb_skill_group(plugin, back_target="cat:work"),
            session,
        )
        return

    if data == "cat:git":
        await _nav_reply(query, "📂 *Git*", _kb_git(), session, parse_mode="Markdown")
        return

    if data == "cat:settings":
        await _nav_reply(query, "⚙ *Settings*", _kb_settings(), session, parse_mode="Markdown")
        return

    if data == "cat:session":
        await _nav_reply(query, "📋 *Session*", _kb_session(), session, parse_mode="Markdown")
        return

    # --- Settings ---
    if data == "set:model":
        await _nav_reply(query, "⚙ *Select model:*", _kb_model_picker(), session, parse_mode="Markdown")
        return

    if data.startswith("set:model:"):
        choice = data[len("set:model:"):]
        if choice == "default":
            _settings.pop("model", None)
        else:
            _settings["model"] = MODEL_ALIASES.get(choice, choice)
        _save_settings()
        await _nav_reply(
            query,
            f"⚙ Model set to *{choice}*",
            _kb_settings(),
            session,
            parse_mode="Markdown",
        )
        return

    if data == "set:sudo":
        if _settings.get("skip_permissions") == "1":
            _settings["skip_permissions"] = "0"
        else:
            _settings["skip_permissions"] = "1"
        _save_settings()
        state = "ON" if _settings.get("skip_permissions") == "1" else "OFF"
        await _nav_reply(
            query,
            f"⚙ Sudo is now *{state}*",
            _kb_settings(),
            session,
            parse_mode="Markdown",
        )
        return

    # --- Session Info (safe to view while busy) ---
    if data == "ses:info":
        s = _get_session(chat_id)
        if not s.session_id:
            await _nav_reply(query, "No active session. Send a message to start one.", _kb_main_menu(chat_id), session)
            return
        model = _settings.get("model", "default")
        sudo = "enabled" if _settings.get("skip_permissions") == "1" else "disabled"
        await _nav_reply(
            query,
            f"📋 *Session Info*\n"
            f"ID: `{s.session_id}`\n"
            f"Started: {s.created_at}\n"
            f"Messages: {s.message_count}\n"
            f"Model: {model}\n"
            f"Sudo: {sudo}",
            _kb_main_menu(chat_id),
            session,
            parse_mode="Markdown",
        )
        return

    # --- Session history & resume ---
    if data == "ses:history" or data.startswith("sh:"):
        page_size = 5
        offset = 0
        if data.startswith("sh:"):
            offset = max(0, int(data[3:]))
        sessions_list, total = _scan_cc_sessions(limit=page_size, offset=offset)
        if not sessions_list:
            await _nav_reply(query, "No sessions found.", _kb_session(), session)
            return
        await _nav_reply(
            query,
            f"📜 *Session History* ({total} total)\nTap to resume:",
            _kb_session_history(sessions_list, session.session_id, offset, total, page_size),
            session,
            parse_mode="Markdown",
        )
        return

    if data.startswith("sr:"):
        target_sid = data[3:]
        session.session_id = target_sid
        session.created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        session.message_count = 0
        _save_sessions()
        # Find the prompt for display
        prompt_text = target_sid[:16]
        for s in _scan_cc_sessions(limit=20)[0]:
            if s["session_id"] == target_sid:
                prompt_text = s["prompt"]
                break
        await _nav_reply(
            query,
            f"📜 Resumed session:\n_{prompt_text}_\n`{target_sid[:16]}...`",
            _kb_main_menu(chat_id),
            session,
            parse_mode="Markdown",
        )
        return

    # Clear any pending skill when navigating menus
    if data not in ("cancel",) and not data.startswith("sk:") and not data.startswith("sg:") and not data.startswith("wg:"):
        session.pending_skill = None

    # --- Skill activation (nonblocking — just sets pending state) ---
    if data.startswith("sk:"):
        skill_name = data[3:]
        session.pending_skill = skill_name
        await _nav_reply(
            query,
            f"🛠 *{skill_name}*\nType your message (it will be sent as `/{skill_name} <your text>`).",
            _kb_cancel(),
            session,
            parse_mode="Markdown",
        )
        return

    # --- Git commands (queue if busy) ---
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
            _record_recent(chat_id, f"git:{action}")
            await _relay(update, immediate[action])
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
            await _nav_reply(
                query,
                f"📂 *git {action}*\n{label}",
                _kb_cancel(),
                session,
                parse_mode="Markdown",
            )
            return

    # --- Session commands ---
    if data.startswith("ses:"):
        action = data[4:]
        if action == "new":
            old = _get_session(chat_id).session_id
            _sessions[chat_id] = Session()
            _save_sessions()
            msg = "🆕 New session started."
            if old:
                msg += f"\nPrevious: `{old[:16]}...`"
            await _nav_reply(query, msg, _kb_main_menu(chat_id), session, parse_mode="Markdown")
            return

        if action == "compact":
            s = _get_session(chat_id)
            if s.busy:
                await update.effective_chat.send_message(
                    "Cannot compact while a request is in progress."
                )
                return
            await query.edit_message_text("📦 Compacting session...")
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
            await _nav_reply(query, "🗑 Session cleared.", _kb_main_menu(chat_id), session)
            return


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
    "/restart — Syntax-check & restart bot\n"
    "/sessions — Browse session history\n"
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
async def cmd_sessions(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Browse Claude Code session history with pagination buttons."""
    page_size = 5
    sessions_list, total = _scan_cc_sessions(limit=page_size, offset=0)
    if not sessions_list:
        await update.message.reply_text("No session history found.")
        return
    session = _get_session(update.effective_chat.id)
    await update.message.reply_text(
        f"📜 *Session History* ({total} total)",
        parse_mode="Markdown",
        reply_markup=_kb_session_history(
            sessions_list, session.session_id, 0, total, page_size
        ),
    )


@_auth
async def cmd_compact(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    s = _get_session(update.effective_chat.id)
    if s.busy:
        await update.message.reply_text("Cannot compact while a request is in progress.")
        return
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


# --- Self-update / restart ---


@_auth
async def cmd_restart(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Syntax-check bot.py then restart via systemd."""
    await update.message.reply_text("Checking syntax...")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "py_compile", str(BOT_DIR / "bot.py"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_out = await proc.communicate()

    if proc.returncode != 0:
        err = stderr_out.decode().strip()
        await update.message.reply_text(
            f"Syntax error — restart aborted:\n```\n{err}\n```",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("Syntax OK. Restarting in 2s...")
    await asyncio.sleep(2)

    # Detached systemd restart — survives our own death.
    # Uses arg-list form (no shell) — safe, no user input involved.
    await asyncio.create_subprocess_exec(
        "systemctl", "--user", "restart", "claude-telegram",
    )


# --- VPS routing ---


@_auth
async def cmd_vps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manage VPS routing: /vps add <name> <work_dir> | /vps remove <name> | /vps"""
    if not ctx.args:
        if not _vps_routes:
            await update.message.reply_text(
                "No VPS routes configured.\n\n"
                "Usage:\n/vps add <name> <work_dir>\n/vps remove <name>"
            )
            return
        lines = [f"/{name} \u2192 {cfg.get('work_dir', '?')}" for name, cfg in _vps_routes.items()]
        await update.message.reply_text("VPS Routes:\n" + "\n".join(lines))
        return

    action = ctx.args[0].lower()
    if action == "add" and len(ctx.args) >= 3:
        name = ctx.args[1].lower()
        work_dir = ctx.args[2]
        _vps_routes[name] = {"work_dir": work_dir}
        _save_vps_routes()
        await update.message.reply_text(f"Added: /{name} \u2192 {work_dir}")
    elif action == "remove" and len(ctx.args) >= 2:
        name = ctx.args[1].lower()
        if name in _vps_routes:
            del _vps_routes[name]
            _save_vps_routes()
            await update.message.reply_text(f"Removed: /{name}")
        else:
            await update.message.reply_text(f"Route '{name}' not found.")
    elif action == "rename" and len(ctx.args) >= 3:
        old_name = ctx.args[1].lower()
        new_name = ctx.args[2].lower()
        if old_name in _vps_routes:
            _vps_routes[new_name] = _vps_routes.pop(old_name)
            _save_vps_routes()
            await update.message.reply_text(f"Renamed: /{old_name} \u2192 /{new_name}")
        else:
            await update.message.reply_text(f"Route '{old_name}' not found.")
    else:
        await update.message.reply_text(
            "Usage:\n/vps add <name> <work_dir>\n/vps remove <name>\n"
            "/vps rename <old> <new>\n/vps (list)"
        )


# --- Regular messages ---


@_auth
async def handle_photo(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages — save image and relay to Claude Code."""
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    photo = update.message.photo[-1]  # largest available size
    tg_file = await photo.get_file()

    PHOTO_DIR.mkdir(exist_ok=True)
    ext = Path(tg_file.file_path).suffix if tg_file.file_path else ".jpg"
    path = PHOTO_DIR / f"{photo.file_unique_id}{ext}"
    await tg_file.download_to_drive(str(path))

    caption = update.message.caption or ""

    if session.pending_skill:
        skill = session.pending_skill
        session.pending_skill = None
        _record_recent(chat_id, skill)
        prompt = (
            f"/{skill} {caption}\n\n"
            f"[User attached an image: {path} — view it with the Read tool]"
        )
    elif caption:
        prompt = (
            f"{caption}\n\n"
            f"[User attached an image: {path} — view it with the Read tool]"
        )
    else:
        prompt = f"The user sent an image. View it with the Read tool: {path}"

    await _relay(update, prompt)


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

    # VPS routing: check for prefix like "/fleet ..." or "/hadrian ..."
    if text.startswith("/") and " " in text:
        prefix = text.split(" ", 1)[0][1:].lower()
        if prefix in _vps_routes:
            text = text.split(" ", 1)[1]
            await _relay(update, text, vps_override=_vps_routes[prefix])
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
            BotCommand("sessions", "Browse session history"),
            BotCommand("restart", "Syntax-check & restart bot"),
            BotCommand("vps", "Manage VPS routing"),
            BotCommand("help", "Show help"),
        ]
    )
    logger.info("Bot commands registered with Telegram")


def main() -> None:
    _acquire_lock()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )

    handlers = [
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("menu", cmd_menu),
        CommandHandler("new", cmd_new),
        CommandHandler("session", cmd_session),
        CommandHandler("sessions", cmd_sessions),
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
        CommandHandler("restart", cmd_restart),
        CommandHandler("vps", cmd_vps),
        CallbackQueryHandler(handle_callback),
        MessageHandler(filters.PHOTO, handle_photo),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
    ]

    for h in handlers:
        app.add_handler(h)

    logger.info("Starting bot (work_dir=%s, claude=%s)", WORK_DIR, CLAUDE_BIN)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
