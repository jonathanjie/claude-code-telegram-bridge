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

**Single-file design** — everything lives in `bot.py` (~1170 lines). This is intentional; the bot is simple enough that splitting into modules would add indirection without value.

### Key Sections in bot.py

1. **Config** (lines 1-60) — .env loading, constants, model aliases
2. **Skill discovery** (lines 70-115) — scans `~/.claude/plugins/` for user-invocable skills at startup
3. **Persistence** (lines 115-245) — owner lock, settings, recents, sessions — all JSON file-backed
4. **Claude runner** (lines 250-305) — `run_claude()` — async subprocess exec, JSON output parsing, timeout handling
5. **Helpers** (lines 305-355) — message splitting, result formatting, typing indicator
6. **Keyboards** (lines 370-455) — inline button builders for the 2-level menu system
7. **Auth** (lines 458-485) — decorators for command and callback handlers
8. **Core relay** (lines 490-780) — `_relay()` and `_relay_from_callback()` — send prompt to Claude, return result
9. **Callback handler** (lines 540-740) — button tap state machine
10. **Commands** (lines 785-1065) — slash command handlers
11. **Message handler** (lines 1070-1100) — plain text with pending-skill prefix support
12. **Entrypoint** (lines 1105-1168) — handler registration, polling startup

### Data Flow

```
Telegram message → auth check → pending_skill prefix? → _relay() → run_claude() → format → reply
Button tap → auth check → callback handler → navigate / set state / _relay_from_callback()
```

### State Files (gitignored)

- `.env` — secrets (TELEGRAM_TOKEN, CLAUDE_WORK_DIR)
- `owner.json` — `{"owner_id": <telegram_user_id>}` — first /start wins
- `sessions.json` — `{chat_id: {session_id, created_at, message_count}}`
- `settings.json` — `{model: "...", skip_permissions: "0"|"1"}`
- `recents.json` — `{chat_id: ["skill1", "skill2", ...]}` — max 5 per user

## Conventions

### Code Style

- **No external deps beyond python-telegram-bot** — keep requirements minimal
- **Async throughout** — all handlers are async, Claude runs as async subprocess
- **Auth decorators** — `@_auth` for commands, `@_auth_callback` for button handlers
- **Underscore-prefixed private functions** — `_relay()`, `_split_message()`, `_btn()`, etc.
- **Inline keyboard builders** — `_kb_*()` functions return `InlineKeyboardMarkup`
- **Callback data format** — `prefix:action[:arg]`, max 64 bytes

### Error Handling

- Claude subprocess failures return `{"is_error": True, "result": "..."}`
- Stale session IDs trigger automatic retry without `--resume`
- Markdown parse failures in Telegram fall back to plain text
- Timeout kills the subprocess and returns a timeout message

### Adding a New Command

1. Write an `@_auth` async handler function
2. Add `CommandHandler("name", handler)` in `main()`
3. If it should appear in autocomplete, add to `_post_init()` (keep total <= 9)
4. If button-accessible, add to the appropriate `_kb_*()` builder and `handle_callback()`

### Adding a New Button Category

1. Add a category button in `_kb_main_menu()`
2. Create a `_kb_<category>()` builder
3. Add `cat:<category>` handling in `handle_callback()`

## Service Management

```bash
systemctl --user restart claude-telegram    # restart
systemctl --user status claude-telegram     # check status
journalctl --user -u claude-telegram -f     # live logs
```

**Warning:** If Claude Code is running via this bot, restarting the service will kill the active Claude session. The bot auto-restarts via `Restart=on-failure`.

## Testing

No test suite currently. To verify changes:

1. `python -c "import bot"` — syntax/import check
2. Restart the service and test via Telegram
3. Check logs: `journalctl --user -u claude-telegram -f`

## Known Limitations

- Single-user only (owner lock)
- No streaming — waits for full Claude response before replying
- No file/image upload support
- Telegram's 4096-char message limit requires chunking long responses
- Markdown formatting can fail on Claude's output (falls back to plain text)
