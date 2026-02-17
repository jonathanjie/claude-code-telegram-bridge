# Button UI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the slash-command-only interface with an inline keyboard button system featuring auto-tracked recents, dynamic skill discovery, and 2-level navigation.

**Architecture:** Single-file modification to `bot.py`. Add CallbackQueryHandler for button navigation, skill discovery function scanning `~/.claude/plugins/`, recents persistence to `recents.json`, and pending_skill state on Session. Edit-in-place menu messages.

**Tech Stack:** python-telegram-bot >= 21.0 (InlineKeyboardButton, InlineKeyboardMarkup, CallbackQueryHandler)

---

### Task 1: Add Imports and Constants

**Files:**
- Modify: `bot.py:1-20` (imports)
- Modify: `bot.py:42-44` (constants)

**Step 1: Add new telegram imports**

Add `InlineKeyboardButton`, `InlineKeyboardMarkup`, `CallbackQueryHandler` to imports.

**Step 2: Add RECENTS_FILE constant**

Add `RECENTS_FILE = BOT_DIR / "recents.json"` alongside other file constants.

---

### Task 2: Skill Discovery

**Files:**
- Modify: `bot.py` (new function after `_claude_env()`)

**Step 1: Add discover_skills() function**

Scans `~/.claude/plugins/installed_plugins.json`, walks each plugin's install path for `skills/*/SKILL.md`, returns list of skill dicts with name, plugin, slash_command.

**Step 2: Add module-level `_skills` list**

Call `discover_skills()` at startup, store in `_skills`.

---

### Task 3: Recents Persistence

**Files:**
- Modify: `bot.py` (new section after settings persistence)

**Step 1: Add recents load/save/update functions**

`_load_recents()`, `_save_recents()`, `_record_recent(chat_id, skill_name)` — max 5 per user, deduped, most-recent-first.

---

### Task 4: Session Pending State

**Files:**
- Modify: `bot.py` Session class

**Step 1: Add pending_skill field to Session.__init__**

`self.pending_skill: str | None = None` — not serialized to disk.

---

### Task 5: Keyboard Builder Functions

**Files:**
- Modify: `bot.py` (new section before command handlers)

**Step 1: Build all keyboard factory functions**

- `_kb_main_menu(chat_id)` — recents row + 4 category buttons
- `_kb_skills()` — flat list of discovered skills + Back
- `_kb_git()` — git commands + Back
- `_kb_settings()` — model/sudo/workdir with current values + Back
- `_kb_session()` — info/new/compact/clear + Back
- `_kb_model_picker()` — opus/sonnet/haiku/default + Back

---

### Task 6: Callback Query Handler

**Files:**
- Modify: `bot.py` (new handler function)

**Step 1: Add handle_callback function**

Routes on callback_data prefix: `menu`, `cat:*`, `sk:*`, `git:*`, `ses:*`, `set:*`, `back`, `cancel`. Uses `query.edit_message_text()` for navigation. Sets `pending_skill` for skill activation.

---

### Task 7: Modify handle_message for Pending Skills

**Files:**
- Modify: `bot.py` handle_message function

**Step 1: Check pending_skill before relaying**

If `session.pending_skill` is set, prefix the message with `/<skill_name>`, clear pending_skill, record recent, then relay.

---

### Task 8: Update /start, /help, Add /menu

**Files:**
- Modify: `bot.py` cmd_start, _post_init, main()

**Step 1: Make /start and /help show the button menu instead of text**

**Step 2: Add cmd_menu handler that shows button menu**

**Step 3: Register CallbackQueryHandler in main()**

**Step 4: Trim set_my_commands to 10 commands**

---

### Task 9: Test and Deploy

**Step 1: Syntax check**

Run: `python3 -c "import ast; ast.parse(open('bot.py').read()); print('OK')"`

**Step 2: Restart service**

Run: `sudo systemctl restart claude-telegram && sleep 2 && sudo systemctl status claude-telegram`
