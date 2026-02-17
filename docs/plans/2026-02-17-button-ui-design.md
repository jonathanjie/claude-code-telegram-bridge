# Button-Based UI for Claude Telegram Bot

**Date:** 2026-02-17
**Status:** Approved

## Problem

The bot has 21 slash commands, exceeding Telegram's 20-command limit for `set_my_commands()`. Adding Claude Code skills (35+ user-invocable skills across 14 plugins) would make this far worse. Slash commands also don't surface discoverability — users must memorize command names.

## Solution

Replace most slash commands with inline keyboard buttons using a 2-level hub menu. Auto-tracked recents provide quick access to frequently used skills.

## Button Architecture

### Main Menu (via `/menu` or `/start`)

```
┌─────────────────────────────────────────┐
│ ⚡ Recent                                │
│ [brainstorming] [frontend-design] [TDD] │
├─────────────────────────────────────────┤
│ [🛠 Skills]     [📂 Git]               │
│ [⚙ Settings]   [📋 Session]            │
└─────────────────────────────────────────┘
```

- Recents row: last 5 skills/commands used, auto-tracked, most-recent-first.
- 4 category buttons open level-2 submenus.

### Level 2: Skills

Flat list of all user-invocable skills discovered from installed plugins. No plugin-name grouping.

### Level 2: Git

status, diff, log, commit, branch, stash, undo, pr

### Level 2: Settings

Model picker (opus/sonnet/haiku), sudo toggle, work dir display. Current values shown on button labels.

### Level 2: Session

Info, New, Compact, Clear.

## Callback Data Schema

64-byte limit per button. Format: `prefix:action[:arg]`

```
menu                    → main menu
cat:<name>              → category submenu (skills, git, settings, session)
sk:<skill>              → activate skill (awaiting-input mode)
git:<action>            → git command (immediate or awaiting-input)
ses:<action>            → session command
set:<key>               → settings submenu
set:<key>:<value>       → set value
back                    → back to main menu
cancel                  → cancel awaiting-input
```

## Awaiting-Input State

When a skill button is tapped, the bot remembers `pending_skill` on the Session. The next plain-text message gets prefixed with `/<skill_name>` and sent to Claude. Cleared on Cancel or another button tap. Ephemeral (not persisted to disk).

## Recents Tracking

Stored in `recents.json`. Per-user list, max 5, most-recent-first. Updated when a skill is actually invoked. Deduped — reusing a skill moves it to front.

## Slash Commands: Keep vs. Demote

**Keep registered (10):** start, help, menu, new, model, sudo, commit, status, diff, run

**Demote to button-only (11):** session, compact, clear, settings, log, branch, stash, undo, pr, find, read, edit

All handlers remain in code — demoted commands still work if typed. They just don't appear in Telegram's autocomplete.

## Skill Discovery

Dynamic at startup. Scans `~/.claude/plugins/installed_plugins.json` and walks `skills/*/SKILL.md` paths. New plugin installs appear automatically after bot restart.

## Menu Message Behavior

- Edit-in-place: navigating between menus edits the existing message, not new messages.
- Auto-dismiss: buttons removed when Claude starts processing.
- `/menu` always creates a fresh menu message.
