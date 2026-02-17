# Claude Code Telegram Bridge

Control [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from your phone via Telegram. Send messages, run git commands, invoke skills, and manage sessions — all through a button-based UI or slash commands.

## How It Works

The bot wraps `claude -p` (Claude Code's CLI print mode) as a Telegram bot. Each message you send becomes a prompt; each response comes back as a Telegram message. Sessions persist across messages, so you get a continuous conversation with full access to your codebase.

```
You (Telegram) → Bot → claude -p --resume <session> → Bot → You (Telegram)
```

## Features

### Button-Based UI

Tap `/menu` or `/start` to get an interactive menu:

```
┌─────────────────────────────────────────┐
│ ⚡ Recent                                │
│ [brainstorming] [frontend-design] [TDD] │
├─────────────────────────────────────────┤
│ [🛠 Skills]     [📂 Git]               │
│ [⚙ Settings]   [📋 Session]            │
└─────────────────────────────────────────┘
```

- **Recents** — your last 3 most-used skills, one tap away
- **Skills** — all Claude Code skills discovered from installed plugins
- **Git** — status, diff, log, commit, branch, stash, undo, PR
- **Settings** — model picker (opus/sonnet/haiku), sudo toggle
- **Session** — info, new, compact (summarize & restart), clear

### Slash Commands

Core commands registered with Telegram autocomplete:

| Command | Description |
|---------|-------------|
| `/menu` | Open button menu |
| `/new` | Fresh session |
| `/model [name]` | Set or show model |
| `/sudo [on\|off]` | Toggle `--dangerously-skip-permissions` |
| `/status` | Git status |
| `/diff` | Git diff |
| `/commit [msg]` | Commit changes |
| `/run <cmd>` | Run a shell command |
| `/help` | Show all commands |

Additional commands work when typed but aren't in autocomplete: `/session`, `/compact`, `/clear`, `/settings`, `/log`, `/branch`, `/stash`, `/undo`, `/pr`, `/find`, `/read`, `/edit`.

### Skill Discovery

At startup, the bot scans `~/.claude/plugins/installed_plugins.json` and discovers all user-invocable skills. These appear in the Skills submenu. Install a new Claude Code plugin, restart the bot, and it shows up automatically.

### Session Management

- **Persistent sessions** — conversations resume across messages via `--resume`
- **Compact** — when context gets long, summarize the conversation and start a fresh session with the summary injected
- **Multiple sessions** — each Telegram chat gets its own independent session

### Security

- **Single-owner lock** — the first user to `/start` the bot becomes the owner; all others are rejected
- **No shell injection** — uses `create_subprocess_exec` (arg-list form), never `shell=True`
- **Token isolation** — bot token lives in `.env`, never committed

## Setup

### Prerequisites

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Install

```bash
git clone https://github.com/jonathanjie/claude-code-telegram-bridge.git
cd claude-code-telegram-bridge

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your Telegram bot token and working directory
```

### Run

```bash
source .venv/bin/activate
python bot.py
```

### Run as a systemd Service

```bash
# Edit claude-telegram.service to match your paths
cp claude-telegram.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-telegram
```

Check status:

```bash
systemctl --user status claude-telegram
journalctl --user -u claude-telegram -f
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_TOKEN` | Yes | — | Bot token from BotFather |
| `CLAUDE_WORK_DIR` | No | `$HOME` | Working directory for Claude Code |
| `CLAUDE_BIN` | No | auto-detect | Path to `claude` binary |
| `COMMAND_TIMEOUT` | No | `300` | Max seconds per Claude invocation |

### Runtime Settings (via bot)

- **Model** — switch between `opus`, `sonnet`, `haiku`, or any model ID
- **Sudo** — toggle `--dangerously-skip-permissions` for unattended operation

Settings persist in `settings.json` across restarts.

## Architecture

```
bot.py              — single-file bot (~1170 lines)
├── Config          — .env loading, constants, model aliases
├── Skill discovery — plugin scanning at startup
├── Persistence     — owner, sessions, settings, recents (JSON files)
├── Claude runner   — async subprocess exec with timeout
├── Keyboards       — inline button builders (main menu, skills, git, settings, session)
├── Callbacks       — button tap handler with state machine
├── Commands        — slash command handlers
└── Message handler — plain text relay with pending-skill support
```

## License

MIT
