# Motherbot extras: delete bot + edit bot
from sqlalchemy import select, delete as sql_delete


async def handle_delete_confirm(cid, text, u, s, bot_obj, motherbot, extella,
                                TelegramClient, decrypt_token, BotExpert, settings):
    yes_words = ('yes', 'y', 'delete', 'yes, delete', 'да', 'удалить')
    if text.lower().strip() in yes_words:
        try:
            raw = decrypt_token(bot_obj.token_encrypted, settings.secret_key)
            await TelegramClient(raw).delete_webhook()
        except Exception:
            pass
        await s.execute(sql_delete(BotExpert).where(BotExpert.bot_id == bot_obj.id))
        await s.delete(bot_obj)
        await s.flush()
        u.state = 'start'; u.pending_bot_id = None; u.pending_key_name = None
        await s.flush()
        msg = '🗑 <b>Bot deleted.</b>' + chr(10) + chr(10)
        msg += 'All settings and experts removed.' + chr(10)
        msg += 'You can recreate it with the same token anytime.' + chr(10) + chr(10)
        msg += 'Use /start to create a new bot.'
        await motherbot.send_message(cid, msg)
    else:
        u.state = 'active'; u.pending_bot_id = None
        await s.flush()
        await motherbot.send_message(cid, '✅ Deletion cancelled. Bot is still active.')


async def handle_edit_description(cid, text, u, s, bot_obj, motherbot, extella,
                                  BotExpert, _is_local, _clean_desc,
                                  _detect_prompt_param, _build_expert_kb):
    await motherbot.send_message(cid, f'🔍 Searching for <i>{text[:60]}</i>...')
    raw = await extella.search_experts(text, limit=30)
    from .motherbot import _dedup_experts
    matches = _dedup_experts(raw, limit=7)
    if not matches:
        msg = '😕 No experts found. Try rephrasing:' + chr(10)
        msg += '• <i>AI assistant chatbot</i>' + chr(10)
        msg += '• <i>image generation</i>' + chr(10)
        msg += '• <i>text translation</i>'
        await motherbot.send_message(cid, msg)
        return
    bot_obj.system_prompt = text
    await s.execute(sql_delete(BotExpert).where(BotExpert.bot_id == bot_obj.id))
    for i, m in enumerate(matches):
        desc = m.get('description', m['name'])
        s.add(BotExpert(
            bot_id=bot_obj.id, expert_name=m['name'],
            display_name=_clean_desc(desc),
            exec_type='local',
            params_json={'__prompt_param__': _detect_prompt_param(m['name'], desc)},
            is_active=True, sort_order=i))
    await s.flush()
    u.state = 'choosing_experts'; u.pending_bot_id = bot_obj.id
    await s.flush()
    selected = {m['name'] for m in matches}
    ed = [{'name': m['name'], 'description': m.get('description', '')} for m in matches]
    legend = chr(10) + chr(10) + '\U0001f4bb All experts run locally on your device via Extella'
    hdr = f'🎯 <b>Found {len(matches)} experts</b>{legend}' + chr(10) + chr(10)
    hdr += 'All selected ✅ — tap to deselect.' + chr(10)
    hdr += 'Ready? Press <b>🚀 Continue</b>'
    await motherbot.send_message(cid, hdr,
        reply_markup=_build_expert_kb(ed, selected, bot_obj.id))
