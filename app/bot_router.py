import logging
import re
from sqlalchemy import select
from .database import Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import decrypt_token
from .config import settings

logger = logging.getLogger(__name__)
extella = ExtellaClient(settings.extella_token)

# Regex to detect API key patterns (safety mask)
_KEY_RE = re.compile(
    r'(sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35,}'
    r'|eyJ[A-Za-z0-9_.-]{30,}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}'
    r'-[a-f0-9]{4}-[a-f0-9]{12})'
)

LANG_PROMPTS = {
    'ru': 'Отвечай только на русском языке.',
    'en': 'Respond only in English.',
    'de': 'Antworte nur auf Deutsch.',
    'fr': 'Reponds uniquement en francais.',
    'es': 'Responde solo en espanol.',
    'uk': 'Відповідай тільки українською.',
    'it': 'Rispondi solo in italiano.',
    'pt': 'Responda apenas em portugues.',
    'zh': '只用中文回答。',
    'ja': '日本語のみで回答してください。',
    'ko': '한국어로만 답변하세요.',
    'tr': 'Sadece Turkce yanit ver.',
    'pl': 'Odpowiadaj tylko po polsku.',
    'ar': 'أجب باللغة العربية فقط.',
}
_DEFAULT_INTENT = {
    'photo':    'обработай это изображение',
    'video':    'опиши это видео',
    'voice':    'транскрибируй голосовое',
    'audio':    'транскрибируй аудиофайл',
    'document': 'обработай документ',
}
_CHAT_ACTION = {
    'text': 'typing', 'photo': 'upload_photo', 'video': 'upload_video',
    'voice': 'record_voice', 'audio': 'upload_voice', 'document': 'upload_document',
}
_MEDIA_HINT = {
    'photo':    'image photo visual processing',
    'video':    'video processing',
    'voice':    'voice audio transcription speech to text',
    'audio':    'audio transcription processing',
    'document': 'document file text extraction',
}

def _detect_lang(msg: dict) -> str:
    lang = (msg.get('from') or {}).get('language_code', '')
    if lang: return lang[:2].lower()
    text = msg.get('text', '') or msg.get('caption', '')
    if text:
        cyr = sum(1 for c in text if '\u0400' <= c <= '\u04FF')
        if len(text) > 0 and cyr / len(text) > 0.3: return 'ru'
    return 'en'

def _inject_lang(params: dict, lang: str) -> dict:
    inst = LANG_PROMPTS.get(lang, f'Respond in {lang} language.')
    if 'system_prompt' in params:
        sp = params.get('system_prompt', '')
        if inst not in sp: params['system_prompt'] = f'{sp}\n{inst}'.strip()
    if 'language' not in params: params['language'] = lang
    return params

def _safe_text(text: str) -> str:
    """Mask any API key patterns before sending to user."""
    return _KEY_RE.sub('[REDACTED]', text)

async def handle_user_bot_update(token_hash: str, data: dict):
    try:
        async with get_session() as session:
            bot = (await session.execute(
                select(Bot).where(Bot.token_hash == token_hash, Bot.is_active == True)
            )).scalar_one_or_none()
            if not bot: logger.warning(f'No bot hash={token_hash}'); return
            raw = decrypt_token(bot.token_encrypted, settings.secret_key)
            utg = TelegramClient(raw)
            if msg := data.get('message'): await _process(utg, bot, msg, session)
            elif cb := data.get('callback_query'): await utg.answer_callback_query(cb['id'])
    except Exception as e:
        logger.error(f'user_bot hash={token_hash}: {e}', exc_info=True)

async def _process(utg, bot, msg: dict, session):
    cid = msg['chat']['id']
    raw_text = msg.get('text', '').strip()
    caption = msg.get('caption', '').strip()
    mt = 'text'; fid = None
    if msg.get('photo'): mt = 'photo'; fid = msg['photo'][-1]['file_id']
    elif msg.get('video'): mt = 'video'; fid = msg['video']['file_id']
    elif msg.get('voice'): mt = 'voice'; fid = msg['voice']['file_id']
    elif msg.get('audio'): mt = 'audio'; fid = msg['audio']['file_id']
    elif msg.get('document'): mt = 'document'; fid = msg['document']['file_id']
    text = caption or raw_text
    if not text and mt != 'text': text = _DEFAULT_INTENT[mt]
    if not text: return
    lang = _detect_lang(msg)
    exps = (await session.execute(
        select(BotExpert).where(BotExpert.bot_id == bot.id, BotExpert.is_active == True)
        .order_by(BotExpert.sort_order))).scalars().all()
    if raw_text in ('/start', '/help'):
        if exps:
            lines = '\n'.join(
                f'{'\u2601\ufe0f' if e.exec_type == "cloud" else "\U0001f4bb"} {e.display_name or e.expert_name}'
                for e in exps)
            local_n = sum(1 for e in exps if e.exec_type == 'local')
            conn_note = ''
            if local_n and not bot.user_target_id:
                conn_note = f'\n\n\u26a0\ufe0f {local_n} \u044d\u043a\u0441\u043f. \u043d\u0443\u0436\u043d\u0430 \U0001f4bb. /connect \u0434\u043b\u044f \u043f\u043e\u0434\u043a\u043b.'
            await utg.send_message(cid,
                f'\U0001f44b Extella AI | {len(exps)} \u0444\u0443\u043d\u043a\u0446\u0438\u0439\n{lines}'
                f'{conn_note}\n\n\u041f\u0440\u043e\u0441\u0442\u043e \u043d\u0430\u043f\u0438\u0448\u0438 \u0447\u0442\u043e \u043d\u0443\u0436\u043d\u043e!')
        else: await utg.send_message(cid, '\U0001f44b \u0411\u043e\u0442 \u043d\u0430\u0441\u0442\u0440\u0430\u0438\u0432\u0430\u0435\u0442\u0441\u044f.')
        return
    if not exps: await utg.send_message(cid, '\u0411\u043e\u0442 \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d.'); return
    furl = None
    if fid:
        furl = await utg.get_file_url(fid)
        if not furl: await utg.send_message(cid, '\u26a0\ufe0f \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u0444\u0430\u0439\u043b.'); return
    await utg.send_chat_action(cid, _CHAT_ACTION.get(mt, 'typing'))
    query = f'{text} {_MEDIA_HINT.get(mt, '')}'.strip()
    best = await _route(exps, query)
    logger.info(f'bot={bot.id} expert={best.expert_name} type={mt} lang={lang} exec={best.exec_type}')
    params = dict(best.params_json or {})
    pp = params.pop('__prompt_param__', 'prompt')
    if furl:
        uk = {'photo': 'image_url', 'video': 'video_url',
              'voice': 'audio_url', 'audio': 'audio_url',
              'document': 'file_url'}.get(mt, 'file_url')
        params[uk] = furl
        if text != _DEFAULT_INTENT.get(mt, '') and pp != uk: params[pp] = text
    else:
        params[pp] = text
    if settings.openai_api_key: params['api_key'] = settings.openai_api_key
    if settings.fal_api_key:
        params['fal_api_key'] = settings.fal_api_key
        params['fal_api_key_value'] = settings.fal_api_key
    params = _inject_lang(params, lang)
    # Cloud vs local routing
    etype = best.exec_type or 'cloud'
    if etype == 'local':
        if not bot.user_target_id or not bot.user_extella_token_enc:
            await utg.send_message(cid,
                f'\u26a0\ufe0f <b>{best.expert_name}</b> \u0442\u0440\u0435\u0431\u0443\u0435\u0442 \U0001f4bb.\n'
                '\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438 \u043c\u0430\u0448\u0438\u043d\u0443 /connect')
            return
        utok = decrypt_token(bot.user_extella_token_enc, settings.secret_key)
        client = ExtellaClient(utok)
        result = await client.run_expert(best.expert_name, params,
                                          timeout=90, target=bot.user_target_id)
    else:
        # Serverless — always no target
        result = await extella.run_expert(best.expert_name, params,
                                           timeout=90, target=None)
    await _respond(utg, cid, result, len(exps) > 1, best.expert_name)

async def _route(exps: list, query: str):
    if len(exps) == 1: return exps[0]
    try:
        ms = await extella.search_experts(query, limit=15)
        by = {e.expert_name: e for e in exps}
        for m in ms:
            if m['name'] in by:
                logger.info(f'Matched {m["name"]} score={m.get("score","?")}')
                return by[m['name']]
    except Exception as e:
        logger.warning(f'Route fail: {e}')
    return exps[0]

async def _respond(utg, cid: int, result: dict, multi: bool, name: str):
    label = f'\U0001f9e0 <i>{name}</i>\n\n' if multi else ''
    # Network / client error
    if result.get('status') == 'error':
        msg = _safe_text(result.get('message', 'Ошибка'))
        await utg.send_message(cid, f'\u26a0\ufe0f {msg}')
        return
    # Still running async (task_id without result after polling)
    if result.get('task_id') and not result.get('result'):
        await utg.send_message(cid, label + '\u23f3 \u0417\u0430\u0434\u0430\u0447\u0430 \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u0430 \u043d\u0430 \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0435. \u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 \u043f\u043e\u044f\u0432\u0438\u0442\u0441\u044f \u043f\u043e\u0441\u043b\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u044f.')
        return
    inner = result.get('result', result)
    if not inner:
        await utg.send_message(cid, label + '\u0411\u0435\u0437 \u043e\u0442\u0432\u0435\u0442\u0430. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439 \u0435\u0449\u0451.')
        return
    if isinstance(inner, dict) and inner.get('status') == 'error':
        msg = _safe_text(inner.get('message', '\u041e\u0448\u0438\u0431\u043a\u0430'))
        await utg.send_message(cid, f'\u26a0\ufe0f {msg}')
        return
    if isinstance(inner, dict):
        iu = (inner.get('result_url') or inner.get('image_url')
               or inner.get('output_url') or inner.get('output_image_url'))
        if iu:
            cap = label + inner.get('message', '\u2705')
            r = await utg.send_photo(cid, iu, caption=cap)
            if not r.get('ok'):
                await utg.send_message(cid, f'{label}<a href="{iu}">\u0421\u043c\u043e\u0442\u0440\u0435\u0442\u044c</a>')
            return
        au = inner.get('audio_url') or inner.get('voice_url') or inner.get('tts_url')
        if au:
            await utg.send_voice(cid, au)
            if label: await utg.send_message(cid, label.strip())
            return
        vu = inner.get('video_url') or inner.get('output_video_url')
        if vu:
            r = await utg.send_video(cid, vu, caption=label)
            if not r.get('ok'):
                await utg.send_message(cid, f'{label}<a href="{vu}">\u0421\u043c\u043e\u0442\u0440\u0435\u0442\u044c</a>')
            return
    # Text fallback — safe, never expose raw dicts
    await utg.send_message(cid, label + _safe_text(_txt(inner)))

def _txt(inner) -> str:
    if isinstance(inner, str): return inner[:4000]
    if isinstance(inner, dict):
        for k in ('answer','translated','post','transcription',
                   'text','content','output','message','Result'):
            v = inner.get(k)
            if v and isinstance(v, str) and len(v) > 5:
                # Skip if it looks like a UUID/task_id
                if len(v) == 36 and v.count('-') == 4: continue
                return v[:4000]
        # Last resort: extract only meaningful string values
        parts = []
        for k, v in inner.items():
            if k in ('execution_log', 'task_id', 'Kwargs', 'expert_name'):
                continue  # NEVER expose these to users
            if isinstance(v, str) and len(v) > 3 and len(v) < 500:
                # Skip API key-like values
                if not _KEY_RE.search(v):
                    parts.append(f'{k}: {v}')
        return '\n'.join(parts)[:2000] if parts else '\u0417\u0430\u0434\u0430\u0447\u0430 \u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u0430.'
    return str(inner)[:500]
