# Nested Skill Groups + Parallel Navigation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restructure the flat Skills button menu into grouped 3-level navigation with emoji labels, and make all menu navigation non-blocking while Claude Code is processing.

**Architecture:** All changes are in `bot.py`. Enhanced `discover_skills()` scans three patterns. A new `SKILL_GROUPS` constant maps plugin names to emoji+label. New keyboard builders replace the flat `_kb_skills()`. A `_nav_reply()` helper makes navigation work even when `session.busy` is True by sending new messages instead of editing.

**Tech Stack:** Python 3, python-telegram-bot (existing)

---

### Task 1: Enhance `discover_skills()` to scan commands and nested skills

**Files:**
- Modify: `bot.py:77-110` (the `discover_skills` function)

**Step 1: Update `discover_skills()` to scan three patterns**

Replace lines 97-106 in `bot.py` (the single `rglob` loop) with scanning three patterns:

```python
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
            # Only pick up if parent.parent.name != "skills" (deeper nesting)
            if skill_md.parent.parent.parent.name == "skills":
                continue
            skill_name = skill_md.parent.name
            if skill_name in seen:
                continue
            seen.add(skill_name)
            skills.append({
                "name": skill_name,
                "plugin": plugin_name,
                "slash": f"/{skill_name}",
            })
```

**Step 2: Verify discovery works**

Run: `cd /home/jons-openclaw/claude-telegram && python3 -c "from bot import _skills; print(f'{len(_skills)} skills'); [print(f'  {s[\"plugin\"]:25s} {s[\"name\"]}') for s in _skills]"`

Expected: More skills than before (was ~14, should now be 25+), including Notion commands and code-review.

**Step 3: Commit**

```bash
cd /home/jons-openclaw/claude-telegram
git add bot.py
git commit -m "feat: enhance skill discovery to scan commands/ and nested skills"
```

---

### Task 2: Add `SKILL_GROUPS` constant and helper

**Files:**
- Modify: `bot.py` — insert after line 113 (`_skills: list[dict] = discover_skills()`)

**Step 1: Add the group mapping and helper**

Insert after `_skills` initialization:

```python
# ---------------------------------------------------------------------------
# Skill group mapping — plugin name → (emoji, display label)
# ---------------------------------------------------------------------------

SKILL_GROUPS: dict[str, tuple[str, str]] = {
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
```

**Step 2: Verify grouping**

Run: `cd /home/jons-openclaw/claude-telegram && python3 -c "from bot import _skills_by_group, _group_label; groups = _skills_by_group(); [print(f'{_group_label(k)}: {len(v)} skills') for k, v in groups.items()]"`

Expected: Output showing each group with emoji label and count.

**Step 3: Commit**

```bash
cd /home/jons-openclaw/claude-telegram
git add bot.py
git commit -m "feat: add SKILL_GROUPS mapping and grouping helpers"
```

---

### Task 3: Replace flat `_kb_skills()` with grouped keyboard builders

**Files:**
- Modify: `bot.py:392-403` — replace `_kb_skills()` with two new functions

**Step 1: Replace `_kb_skills()` with `_kb_skill_groups()` and `_kb_skill_group()`**

Delete the old `_kb_skills()` (lines 392-403) and replace with:

```python
def _kb_skill_groups() -> InlineKeyboardMarkup:
    """Skills menu — shows plugin groups with emoji and skill count."""
    groups = _skills_by_group()
    rows: list[list[InlineKeyboardButton]] = []
    # Sort groups: larger groups first, then alphabetical
    sorted_groups = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    # Groups with >1 skill get their own row; singletons pair up
    singles: list[tuple[str, list[dict]]] = []
    for plugin, skills in sorted_groups:
        label = _group_label(plugin)
        count = len(skills)
        if count == 1:
            singles.append((plugin, skills))
        else:
            rows.append([_btn(f"{label} ({count})", f"grp:{plugin}")])

    # Pair up single-skill groups
    pair: list[InlineKeyboardButton] = []
    for plugin, skills in singles:
        # Single-skill groups bypass to skill directly
        pair.append(_btn(_group_label(plugin), f"sk:{skills[0]['name']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)

    rows.append([_btn("« Back", "back")])
    return InlineKeyboardMarkup(rows)


def _kb_skill_group(plugin: str) -> InlineKeyboardMarkup:
    """Show skills within a specific plugin group."""
    groups = _skills_by_group()
    skills = groups.get(plugin, [])
    rows: list[list[InlineKeyboardButton]] = []

    if len(skills) <= 3:
        # Single column for small groups
        for sk in skills:
            rows.append([_btn(sk["name"], f"sk:{sk['name']}")])
    else:
        # Two-column grid
        pair: list[InlineKeyboardButton] = []
        for sk in skills:
            pair.append(_btn(sk["name"], f"sk:{sk['name']}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)

    rows.append([_btn("« Back", "back:skills")])
    return InlineKeyboardMarkup(rows)
```

**Step 2: Verify keyboards render**

Run: `cd /home/jons-openclaw/claude-telegram && python3 -c "from bot import _kb_skill_groups, _kb_skill_group; kg = _kb_skill_groups(); print('Groups:', [[b.text for b in row] for row in kg.inline_keyboard]); ks = _kb_skill_group('superpowers'); print('Superpowers:', [[b.text for b in row] for row in ks.inline_keyboard])"`

Expected: Groups keyboard shows emoji-labeled buttons with counts. Superpowers keyboard shows individual skill names in 2-column grid.

**Step 3: Commit**

```bash
cd /home/jons-openclaw/claude-telegram
git add bot.py
git commit -m "feat: replace flat skill list with grouped keyboard builders"
```

---

### Task 4: Add `_nav_reply()` helper for busy-aware navigation

**Files:**
- Modify: `bot.py` — insert before `handle_callback` (around line 538)

**Step 1: Add the helper function**

Insert before the callback handler section:

```python
async def _nav_reply(
    query,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    session: Session,
    *,
    parse_mode: str | None = None,
) -> None:
    """Send a navigation response — edit if idle, new message if busy.

    When Claude is busy processing, the original menu message may have been
    replaced by a status message. Sending a new message avoids conflicts
    and lets the user browse freely while waiting.
    """
    if session.busy:
        await query.message.chat.send_message(
            text, reply_markup=reply_markup, parse_mode=parse_mode,
        )
    else:
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode,
        )
```

**Step 2: Verify it parses**

Run: `cd /home/jons-openclaw/claude-telegram && python3 -c "import bot; print('OK')"`

Expected: `OK`

**Step 3: Commit**

```bash
cd /home/jons-openclaw/claude-telegram
git add bot.py
git commit -m "feat: add _nav_reply helper for busy-aware menu navigation"
```

---

### Task 5: Update `handle_callback()` — navigation uses `_nav_reply`, add group routing

**Files:**
- Modify: `bot.py:545-637` (the `handle_callback` function)

**Step 1: Update navigation callbacks to use `_nav_reply` and add group handling**

In `handle_callback`, make these changes:

1. **Replace `back` handler** (line 558-560):

```python
    # --- Navigation ---
    if data == "menu" or data == "back":
        await _nav_reply(query, MENU_TEXT, _kb_main_menu(chat_id), session)
        return
```

2. **Add `back:skills` handler** right after `back`:

```python
    if data == "back:skills":
        await _nav_reply(query, "🛠 *Skills*\nPick a group:", _kb_skill_groups(), session, parse_mode="Markdown")
        return
```

3. **Update `cancel` handler** (line 565-568):

```python
    if data == "cancel":
        session.pending_skill = None
        await _nav_reply(query, MENU_TEXT, _kb_main_menu(chat_id), session)
        return
```

4. **Update `cat:skills`** (line 571-573) — use `_nav_reply` and `_kb_skill_groups()`:

```python
    if data == "cat:skills":
        await _nav_reply(query, "🛠 *Skills*\nPick a group:", _kb_skill_groups(), session, parse_mode="Markdown")
        return
```

5. **Add `grp:*` handler** right after `cat:session`:

```python
    # --- Skill group ---
    if data.startswith("grp:"):
        plugin = data[4:]
        label = _group_label(plugin)
        await _nav_reply(query, f"{label}\nTap to activate, then type your message.", _kb_skill_group(plugin), session)
        return
```

6. **Update other navigation callbacks** (`cat:git`, `cat:settings`, `cat:session`) to use `_nav_reply`:

```python
    if data == "cat:git":
        await _nav_reply(query, "📂 *Git*", _kb_git(), session, parse_mode="Markdown")
        return

    if data == "cat:settings":
        await _nav_reply(query, "⚙ *Settings*", _kb_settings(), session, parse_mode="Markdown")
        return

    if data == "cat:session":
        await _nav_reply(query, "📋 *Session*", _kb_session(), session, parse_mode="Markdown")
        return
```

7. **Update `sk:*` handler** (line 588-596) to use `_nav_reply`:

```python
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
```

8. **Update git input prompts** (line 621-629) to use `_nav_reply`:

```python
        if action in prompts:
            label, tag = prompts[action]
            session.pending_skill = tag
            await _nav_reply(
                query,
                f"📂 *git {action}*\n{label}",
                _kb_cancel(),
                session,
                parse_mode="Markdown",
            )
            return
```

9. **Update session info/new/clear** and **settings** handlers that show menus — use `_nav_reply` for their menu-returning replies (lines 637, 660, 703, and settings responses).

**Step 2: Also update `pending_skill` clearing to ignore `grp:*`**

Update line 554:

```python
    if data not in ("cancel",) and not data.startswith("sk:") and not data.startswith("grp:"):
        session.pending_skill = None
```

**Step 3: Verify the bot parses**

Run: `cd /home/jons-openclaw/claude-telegram && python3 -c "import bot; print('OK')"`

Expected: `OK`

**Step 4: Commit**

```bash
cd /home/jons-openclaw/claude-telegram
git add bot.py
git commit -m "feat: grouped skill navigation with busy-aware _nav_reply"
```

---

### Task 6: Update CLAUDE.md with new architecture info

**Files:**
- Modify: `CLAUDE.md` — update keyboard builders section and add nav info

**Step 1: Update CLAUDE.md**

Update the "Key Sections" and "Keyboards" descriptions to reflect:
- `_kb_skill_groups()` and `_kb_skill_group()` replace `_kb_skills()`
- `_nav_reply()` helper for busy-aware navigation
- `SKILL_GROUPS` constant
- `grp:*` and `back:skills` callback data
- Discovery now scans `commands/` and nested skill patterns

**Step 2: Commit**

```bash
cd /home/jons-openclaw/claude-telegram
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for nested skills and parallel nav"
```

---

### Task 7: Manual integration test

**Step 1: Restart the bot service**

```bash
systemctl --user restart claude-telegram
```

**Step 2: Test in Telegram**

1. Send `/menu` — verify main menu appears with Skills/Git/Settings/Session
2. Tap **🛠 Skills** — verify grouped list with emoji labels and counts
3. Tap **💥 Superpowers (14)** — verify 2-column grid of superpowers skills
4. Tap **« Back** — verify returns to groups list
5. Tap **« Back** — verify returns to main menu
6. Tap a single-skill group (e.g. **🎨 Frontend**) — verify it activates the skill directly
7. Start a long-running Claude command (e.g. type a complex question)
8. While Claude is processing, tap `/menu` and navigate Skills → groups → back — verify navigation works without blocking (new messages appear)
9. Verify the Claude response still comes through normally

**Step 3: Check logs for errors**

```bash
journalctl --user -u claude-telegram --since "5 min ago" --no-pager
```
