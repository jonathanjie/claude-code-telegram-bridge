# Nested Skill Groups + Parallel Navigation

**Date:** 2026-02-17
**Status:** Approved

## Problem

1. The Skills menu is a flat list of 14+ skills from multiple plugins — unwieldy to scroll on a phone.
2. Button navigation blocks when Claude Code is processing — you can't browse the menu while waiting for a response.

## Design Decisions

- **3-level menu** for skills: Main Menu → Skills → Plugin Group → individual skills
- **Static group mapping** with emoji labels, fallback for unknown plugins
- **Enhanced skill discovery** to also scan `commands/*.md` (currently missed)
- **Navigation always instant** regardless of `session.busy`; only command execution serializes

---

## Feature 1: Nested Skill Groups

### Skill Discovery Enhancement

Current `discover_skills()` only finds `skills/*/SKILL.md`. Many plugins use `commands/*.md` instead (code-review, pr-review-toolkit, Notion commands). The Notion plugin also nests skills under `skills/notion/*/SKILL.md`.

Enhanced discovery scans three patterns:
1. `skills/*/SKILL.md` — superpowers, claude-md-management (existing)
2. `commands/*.md` and `commands/*/*.md` — code-review, pr-review-toolkit, Notion commands
3. `skills/*/*/SKILL.md` — Notion deep skills (knowledge-capture, meeting-intelligence, etc.)

Returns `{"name": str, "plugin": str, "slash": str}` unchanged — the `plugin` field is already captured but was unused in the UI.

### Group Mapping

```python
SKILL_GROUPS = {
    "superpowers":          ("💥", "Superpowers"),
    "Notion":               ("📓", "Notion"),
    "atlassian":            ("🏢", "Atlassian"),
    "frontend-design":      ("🎨", "Frontend"),
    "feature-dev":          ("🔧", "Feature Dev"),
    "code-review":          ("🔍", "Code Review"),
    "pr-review-toolkit":    ("📋", "PR Review"),
    "claude-md-management": ("📝", "Project Docs"),
    "code-simplifier":      ("✨", "Simplifier"),
}
```

Unknown plugins fall back to `("🔌", plugin_name.replace("-", " ").title())`.

### Menu Flow

```
Main Menu
  ├─ ⚡ recent1  ⚡ recent2  ⚡ recent3     (recents row, unchanged)
  ├─ 🛠 Skills   📂 Git
  └─ ⚙ Settings  📋 Session

        ↓ tap Skills

🛠 Skills — pick a group:
  ├─ 💥 Superpowers (14)
  ├─ 📓 Notion (12)
  ├─ 🏢 Atlassian (5)
  ├─ 🎨 Frontend (1)    🔧 Feature Dev (1)
  ├─ 🔍 Code Review (1) 📋 PR Review (1)
  ├─ 📝 Project Docs (2) ✨ Simplifier (1)
  └─ « Back

        ↓ tap Superpowers

💥 Superpowers
  ├─ brainstorming        | writing-plans
  ├─ executing-plans      | TDD
  ├─ systematic-debugging | verification
  ├─ ...
  └─ « Back
```

### Callback Data Scheme

- `grp:<plugin>` — open a plugin group (e.g. `grp:superpowers`)
- `sk:<name>` — activate a skill (unchanged)
- `back:skills` — go back to skills group list
- `back` — go back to main menu (unchanged)

Groups with only 1 skill bypass the group page and activate directly.

### Layout Rules for Group Pages

- Groups with ≤ 3 skills: single-column (one skill per row)
- Groups with > 3 skills: two-column grid (two skills per row)
- Groups page itself: single-column if ≤ 4 groups, two-column for groups with count=1 paired together

---

## Feature 2: Parallel Navigation

### Current State

- `_relay()` and `_relay_from_callback()` check `session.busy` and reject if True
- `handle_callback()` does NOT check `session.busy` for navigation (cat:*, back, set:*)
- Navigation already edits the same message via `query.edit_message_text()`

Navigation already doesn't block on `session.busy`. The actual issue is that when Claude is busy, the original menu message may have been replaced by a "Running..." status, so `edit_message_text` fails or edits the wrong message.

### Fix

When `session.busy` is True and a user taps a navigation button:
- Use `send_message` (new message) instead of `edit_message_text`
- This way users can browse freely — new keyboard messages appear while the old "Running..." message stays intact

When `session.busy` is False (idle):
- Use `edit_message_text` as before (cleaner, no message spam)

Implementation: extract a helper `_nav_reply(query, text, markup, session)` that picks the right method.

### Action Callbacks While Busy

- Skill activation (`sk:*`) → still shows the "type your message" prompt immediately (this is navigation)
- The actual command execution happens when the user types — `_relay()` checks `session.busy` and rejects if busy
- Git immediate commands (`git:status`, etc.) → blocked by `_relay_from_callback()` busy check as before, user sees "still working" message

This means no queue is needed. The existing `session.busy` serialization is sufficient. Users can navigate freely, and the natural "type then send" flow for skills means commands only execute when the user explicitly acts.

---

## Files Modified

- `bot.py` — all changes in one file:
  - `discover_skills()` — enhanced to scan commands + nested skills
  - `SKILL_GROUPS` — new constant
  - `_kb_skills()` → `_kb_skill_groups()` — shows plugin groups
  - New `_kb_skill_group(plugin)` — shows skills within a group
  - `handle_callback()` — add `grp:*` and `back:skills` handling
  - `_nav_reply()` — new helper for busy-aware message sending
  - Update all navigation callbacks to use `_nav_reply()`
