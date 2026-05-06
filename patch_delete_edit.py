#!/usr/bin/env python3
"""Patch motherbot.py: add delete bot + edit bot features."""
import sys, re
from pathlib import Path

repo = Path(__file__).parent
mb = repo / "app" / "motherbot.py"
src = mb.read_text(encoding="utf-8")

# ── New handler functions ──────────────────────────────────────────────────

DELETE_CONFIRM_FN = """
async def _handle_delete_confirm(cid, text, u, s):
    from sqlalchemy import select, delete as sql_delete
    bid = u.pending_bot_id
    if not bid:
        await motherbot.send_message(cid, "/start"); return
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if not bot:
        await motherbot.send_message(cid, "/start"); return
    confirmed = text.lower().strip() in ("yes", "y", "delete", "yes, delete", "\u0434\u0430", "\u0443\u0434\u0430\u043b\u0438\u0442\u044c")
    if confirmed:
        try:
            raw = decrypt_token(bot.token_encrypted, settings.secret_key)
            await TelegramClient(raw).delete_webhook()
        except Exception:
            pass
        await s.execute(sql_delete(BotExpert).where(BotExpert.bot_id == bid))
        await s.delete(bot)
        await s.flush()
        u.state = "start"; u.pending_bot_id = None; u.pending_key_name = None
        await s.flush()
        await motherbot.send_message(cid,
            "\U0001f5d1 <b>Bot deleted.</b>\n\n"
            "All settings and experts removed.\n"
            "You can recreate it anytime with the same token.\n\n"
            "Use /start to create a new bot.")
    else:
        u.state = "active"; u.pending_bot_id = None
        await s.flush()
        await motherbot.send_message(cid, "\u2705 Deletion cancelled. Bot is still active.")

"""

EDIT_DESC_FN = """
async def _handle_edit_description(cid, text, u, s):
    from sqlalchemy import select, delete as sql_delete
    bid = u.pending_bot_id
    if not bid:
        await motherbot.send_message(cid, "/start"); return
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if not bot:
        await motherbot.send_message(cid, "/start"); return
    await motherbot.send_message(cid, f"\U0001f50d Searching for <i>{text[:60]}</i>...")
    matches = await extella.search_experts(text, limit=7)
    if not matches:
        await motherbot.send_message(cid,
            "\U0001f615 No experts found. Try rephrasing:\n"
            "\u2022 <i>AI assistant chatbot</i>\n"
            "\u2022 <i>image processing</i>\n"
            "\u2022 <i>text translation</i>")
        return
    bot.system_prompt = text
    await s.execute(sql_delete(BotExpert).where(BotExpert.bot_id == bid))
    local_count = 0
    for i, m in enumerate(matches):
        desc = m.get("description", m["name"])
        is_loc = _is_local(m["name"], desc)
        if is_loc: local_count += 1
        s.add(BotExpert(bot_id=bid, expert_name=m["name"],
                        display_name=_clean_desc(desc),
                        exec_type="local" if is_loc else "cloud",
                        params_json={"__prompt_param__": _detect_prompt_param(m["name"], desc)},
                        is_active=True, sort_order=i))
    await s.flush()
    u.state = "choosing_experts"; u.pending_bot_id = bid
    await s.flush()
    selected = {m["name"] for m in matches}
    exps_dicts = [{"name": m["name"], "description": m.get("description", "")} for m in matches]
    legend = (f"\n\n\u2601\ufe0f = cloud  \U0001f4bb = device ({local_count})"
              if local_count > 0 else "\n\n\u2601\ufe0f All on Extella cloud!")
    await motherbot.send_message(cid,
        f"\U0001f3af <b>Found {len(matches)} experts</b>{legend}\n\n"
        "All selected \u2705 \u2014 tap to deselect.\n"
        "Ready? Press <b>\U0001f680 Continue</b>",
        reply_markup=_build_expert_kb(exps_dicts, selected, bid))

"""

# ── Callback handlers ──────────────────────────────────────────────────────

EDIT_CB = """
        elif action == "edit" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, "Not found"); return
            u.state = "waiting_edit_description"; u.pending_bot_id = bid
            await s.flush(); await motherbot.answer_callback_query(cbid)
            exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
            current = ", ".join(e.display_name or e.expert_name for e in exps[:3])
            await motherbot.send_message(cid,
                f"\u270f\ufe0f <b>Edit @{bot.bot_username}</b>\n\n"
                f"Current functions: <i>{current}</i>\n\n"
                "Describe new functionality and I\'ll find matching experts:\n\n"
                "\u270d\ufe0f <b>What should your bot do now?</b>")

        elif action == "delete_bot" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, "Not found"); return
            u.state = "waiting_delete_confirm"; u.pending_bot_id = bid
            await s.flush(); await motherbot.answer_callback_query(cbid)
            await motherbot.send_message(cid,
                f"\U0001f5d1 <b>Delete @{bot.bot_username}?</b>\n\n"
                "This will remove all settings, experts and webhook.\n"
                "You can recreate it later with the same token.\n\n"
                "Type <b>yes, delete</b> to confirm:",
                reply_markup={"inline_keyboard": [[
                    {"text": "\u274c Cancel", "callback_data": "cancel_key"}]]})

"""

# ── Apply patches ──────────────────────────────────────────────────────────
errors = []

# 1. Add state handlers after waiting_api_key_input
OLD1 = "        elif u.state == \'waiting_api_key_input\':"
# Search without escaped quotes
idx = src.find("elif u.state == \'waiting_api_key_input\':")
if idx == -1:
    idx = src.find('elif u.state == "waiting_api_key_input":')
    marker = 'elif u.state == "waiting_api_key_input":'
    handler_call = (
        '            await _handle_api_key_input(cid, text, u, s)\n'
        '        elif u.state == "waiting_delete_confirm":\n'
        '            await _handle_delete_confirm(cid, text, u, s)\n'
        '        elif u.state == "waiting_edit_description":\n'
        '            await _handle_edit_description(cid, text, u, s)'
    )
else:
    marker = "elif u.state == \'waiting_api_key_input\':"
    handler_call = (
        "            await _handle_api_key_input(cid, text, u, s)\n"
        "        elif u.state == \'waiting_delete_confirm\':\n"
        "            await _handle_delete_confirm(cid, text, u, s)\n"
        "        elif u.state == \'waiting_edit_description\':\n"
        "            await _handle_edit_description(cid, text, u, s)"
    )

# Simple approach: find the api_key_input handler block and add after it
src_lines = src.splitlines()
insert_after = None
for i, ln in enumerate(src_lines):
    if "await _handle_api_key_input(" in ln:
        insert_after = i
        break

if insert_after is None:
    errors.append("Cannot find _handle_api_key_input call")
else:
    new_lines = [
        "        elif u.state == \"waiting_delete_confirm\":",
        "            await _handle_delete_confirm(cid, text, u, s)",
        "        elif u.state == \"waiting_edit_description\":",
        "            await _handle_edit_description(cid, text, u, s)",
    ]
    src_lines = src_lines[:insert_after+1] + new_lines + src_lines[insert_after+1:]
    print(f"[1/4] Added state handlers after line {insert_after}")

# 2. Insert handler functions before _do_activate
do_activate = None
for i, ln in enumerate(src_lines):
    if "async def _do_activate(" in ln:
        do_activate = i; break
if do_activate is None:
    errors.append("Cannot find _do_activate")
else:
    insert_code = (DELETE_CONFIRM_FN + EDIT_DESC_FN).splitlines()
    src_lines = src_lines[:do_activate] + insert_code + src_lines[do_activate:]
    print(f"[2/4] Inserted handler functions at line {do_activate}")

# 3. Add Edit/Delete buttons to manage keyboard
src = "\n".join(src_lines)
OLD_MANAGE = '[{"text": "\\U0001f517 Connect Device/Server",'
if OLD_MANAGE not in src:
    OLD_MANAGE = '"\\U0001f517 Connect Device/Server"'
EDIT_BTN = '[{"text": "\\u270f\\ufe0f Edit Bot Functions", "callback_data": f"edit|{bid}"}],\n                '
if OLD_MANAGE in src:
    src = src.replace(OLD_MANAGE, EDIT_BTN + OLD_MANAGE, 1)
    print("[3/4] Added Edit button")
else:
    errors.append(f"Cannot find manage keyboard: {OLD_MANAGE[:50]}")

# Add Delete button after Deactivate button
OLD_DEACT_BTN = '"\\u23f8\\ufe0f Deactivate"'
if OLD_DEACT_BTN not in src:
    OLD_DEACT_BTN = '"\\U0001f5d1 Deactivate"'
if OLD_DEACT_BTN not in src:
    OLD_DEACT_BTN = '"Deactivate"'
DELETE_BTN = '},\n                [{"text": "\\U0001f5d1 Delete Bot", "callback_data": f"delete_bot|{bid}"}]'
if OLD_DEACT_BTN in src:
    src = src.replace(OLD_DEACT_BTN, OLD_DEACT_BTN + DELETE_BTN, 1)
    print("[3/4] Added Delete button")

# 4. Insert edit/delete callbacks before deactivate callback
src_lines = src.splitlines()
deactivate_cb = None
for i, ln in enumerate(src_lines):
    if 'action == "deactivate" and len(parts) == 2:' in ln:
        deactivate_cb = i; break
if deactivate_cb is None:
    errors.append("Cannot find deactivate callback")
else:
    cb_lines = EDIT_CB.splitlines()
    src_lines = src_lines[:deactivate_cb] + cb_lines + src_lines[deactivate_cb:]
    print(f"[4/4] Inserted callbacks at line {deactivate_cb}")

src = "\n".join(src_lines)

# Validate syntax
import ast
try:
    ast.parse(src)
    print("Syntax OK!")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    errors.append(f"Syntax: {e}")

if errors:
    print("ERRORS:", errors)
    sys.exit(1)

mb.write_text(src, encoding="utf-8")
print(f"Written {len(src)} chars to {mb}")
