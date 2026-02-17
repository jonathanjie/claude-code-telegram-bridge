# CLAUDE.md — Claude Code Telegram Bridge

## Project Overview

A Telegram bot that bridges Claude Code's CLI (`claude -p`) to Telegram, enabling remote control of Claude Code from a phone. Single-file Python bot with button-based UI, session management, and dynamic skill discovery.

## Quick Start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in TELEGRAM_TOKEN
python bot.py
```

## Architecture

**Single-file design** — everything lives in `bot.py` (~1400 lines). This is intentional; the bot is simple enough that splitting into modules would add indirection without value.

### Key Sections in bot.py

1. **Config** (lines 1-60) — .env loading, constants, model aliases
2. **Instance lock** (lines 71-93) — `_acquire_lock()` — `fcntl.flock` PID guard prevents dual instances
3. **Skill discovery** (lines 95-170) — scans `~/.claude/plugins/` for user-invocable skills at startup (3 patterns)
4. **Skill groups** (lines 175-215) — `SKILL_GROUPS` constant + `_group_label()`, `_skills_by_group()` helpers
5. **Persistence** (lines 220-350) — owner lock, settings, recents, sessions — all JSON file-backed
6. **CPU watchdog** (lines 352-390) — `_cpu_watchdog()` — kills stale subprocesses with 0 CPU activity
7. **Claude runner** (lines 392-440) — `run_claude()` — async subprocess exec, JSON output parsing, timeout + watchdog
8. **Helpers** (lines 442-500) — message splitting, result formatting, typing indicator
9. **Keyboards** (lines 502-610) — inline button builders for the 3-layer drill-down menu (Main Menu → Plugin Groups → Skills)
10. **Auth** (lines 612-640) — decorators for command and callback handlers
11. **Core relay** (lines 642-700) — `_relay()` and `_relay_from_callback()`
12. **Nav helper** (lines 703-730) — `_nav_reply()` — busy-aware response (edit if idle, new message if busy)
13. **Callback handler** (lines 730-950) — button tap state machine
14. **Commands** (lines 960-1310) — slash command handlers including `/restart`
15. **Message handler** (lines 1320-1350) — plain text with pending-skill prefix support
16. **Entrypoint** (lines 1360-1415) — `_acquire_lock()`, handler registration, polling startup

### Data Flow

```
Telegram message → auth check → pending_skill prefix? → _relay() → run_claude() → format → reply
Button tap → auth check → callback handler → _nav_reply() / _relay_from_callback()
```

### Self-Update via Telegram

The bot can update its own code through Claude Code:

1. Send a message like "Add feature X to bot.py" → Claude edits `bot.py`
2. Review the response showing changes
3. Send `/restart` → bot syntax-checks → systemd restart with new code

Safety: `/restart` runs `py_compile` before restarting. `Restart=always` in systemd recovers from runtime crashes.

### State Files (gitignored)

- `.env` — secrets (TELEGRAM_TOKEN, CLAUDE_WORK_DIR, optional: CLAUDE_BIN, COMMAND_TIMEOUT, STALE_TIMEOUT)
- `owner.json` — `{"owner_id": <telegram_user_id>}` — first /start wins
- `sessions.json` — `{chat_id: {session_id, created_at, message_count}}`
- `settings.json` — `{model: "...", skip_permissions: "0"|"1"}`
- `recents.json` — `{chat_id: ["skill1", "skill2", ...]}` — max 5 per user
- `bot.lock` — `fcntl.flock` instance guard (PID inside)

## Conventions

### Code Style

- **No external deps beyond python-telegram-bot** — keep requirements minimal
- **Async throughout** — all handlers are async, Claude runs as async subprocess
- **Auth decorators** — `@_auth` for commands, `@_auth_callback` for button handlers
- **Underscore-prefixed private functions** — `_relay()`, `_split_message()`, `_btn()`, etc.
- **Inline keyboard builders** — `_kb_*()` functions return `InlineKeyboardMarkup`
- **Callback data format** — `prefix:action[:arg]`, max 64 bytes. Prefixes: `cat:` (category), `sk:` (invoke skill), `sg:` (skill group drilldown), `set:` (settings), `ses:` (session ops)

### Error Handling

- Claude subprocess failures return `{"is_error": True, "result": "..."}`
- Stale session IDs trigger automatic retry without `--resume`
- Markdown parse failures in Telegram fall back to plain text
- Timeout kills the subprocess and returns a timeout message
- CPU watchdog kills subprocesses with 0 CPU for `STALE_TIMEOUT` seconds (default 60)
- Instance lock prevents dual-instance Telegram Conflict errors

### Adding a New Command

1. Write an `@_auth` async handler function
2. Add `CommandHandler("name", handler)` in `main()`
3. If it should appear in autocomplete, add to `_post_init()` (keep total <= 9)
4. If button-accessible, add to the appropriate `_kb_*()` builder and `handle_callback()`

### Adding a New Button Category

1. Add a category button in `_kb_main_menu()`
2. Create a `_kb_<category>()` builder
3. Add `cat:<category>` handling in `handle_callback()`
4. Use `_nav_reply()` instead of `query.edit_message_text()` for busy-aware navigation

## Service Management

```bash
systemctl --user restart claude-telegram    # restart
systemctl --user status claude-telegram     # check status
journalctl --user -u claude-telegram -f     # live logs
```

The bot uses `Restart=always` and an `fcntl.flock` PID lock. If a second instance tries to start, it exits immediately. Systemd will always restart the bot after any exit.

## Testing

No test suite currently. To verify changes:

1. `python -m py_compile bot.py` — syntax check
2. `python -c "from bot import _acquire_lock; _acquire_lock()"` — verify lock (fails if instance running = good)
3. `/restart` via Telegram — syntax-checks and restarts with new code
4. Check logs: `journalctl --user -u claude-telegram -f`

## Known Limitations

- Single-user only (owner lock)
- No streaming — waits for full Claude response before replying
- No file/image upload support
- Telegram's 4096-char message limit requires chunking long responses
- Markdown formatting can fail on Claude's output (falls back to plain text)
- CPU watchdog reads `/proc/<pid>/stat` — Linux only
