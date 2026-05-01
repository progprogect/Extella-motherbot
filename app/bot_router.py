import logging, re
from sqlalchemy import select
from .database import Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import decrypt_token
from .config import settings
from .cloud_runners import CLOUD_RUNNERS
from .key_manager import get_bot_keys, RUNNER_KEYS

logger = logging.getLogger(__name__)
extella = ExtellaClient(settings.extella_token)

_KEY_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35,}"
    r"|eyJ[A-Za-z0-9_.-]{30,}|aafd[A-Za-z0-9_-]{25,}"
    r"|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"
)
_KNOWN_LOCAL = {
    "image_enhance","improve_photo_quality","remove_background_local","remove_bg_local",
    "video_enhance","video_upscale","text_to_speech","transcribe_audio_file",
    "pdf_edit","edit_pdf","merge_pdf","split_pdf","organize_files","convert_file",
}
_CLOUD_ALT = {
    "image_enhance":          "mb_image_enhance_cloud",
    "improve_photo_quality":  "mb_image_enhance_cloud",
    "remove_background_local":"mb_remove_background_cloud",
    "remove_bg_local":        "mb_remove_background_cloud",
    "transcribe_audio_file":  "mb_transcribe_voice",
}
_LANG = {
    "ru":"Отвечай только на русском языке.","en":"Respond only in English.",
    "de":"Antworte nur auf Deutsch.","fr":"Reponds uniquement en francais.",
    "es":"Responde solo en espanol.","uk":"Відповідай тільки українською.",
    "it":"Rispondi solo in italiano.","pt":"Responda apenas em portugues.",
    "zh":"只用中文回答。","ja":"日本語のみで回答してください。",
    "ko":"한국어로만 답변하세요.","tr":"Sadece Turkce yanit ver.",
    "pl":"Odpowiadaj tylko po polsku.","ar":"أجب باللغة العربية فقط.",
}
_DEFAULT_INTENT = {
    "photo":"обработай это изображение","video":"опиши это видео",
    "voice":"транскрибируй голосовое","audio":"транскрибируй аудиофайл",
    "document":"обработай документ",
}
_CHAT_ACTION = {
    "text":"typing","photo":"upload_photo","video":"upload_video",
    "voice":"record_voice","audio":"upload_voice","document":"upload_document",
}
_MEDIA_HINT = {
    "photo":"image photo visual processing enhance quality",
    "video":"video processing","voice":"voice audio transcription speech",
    "audio":"audio transcription","document":"document file text",
}
_HIDDEN = {"execution_log","task_id","Kwargs","kwargs","expert_name","api_key",
           "fal_api_key","fal_api_key_value","language","system_prompt","__prompt_param__",
           "openai_api_key","fal_api_key","status"}


def _is_local(name: str, desc: str = "") -> bool:
    if name.lower() in _KNOWN_LOCAL: return True
    t = (name + " " + desc).lower()
    return any(w in t for w in ["pillow","opencv","ffmpeg","rembg","ollama",
                                 "output_path","saves to","local file","no api key needed"])


def _detect_lang(msg: dict) -> str:
    lang = (msg.get("from") or {}).get("language_code", "")
    if lang: return lang[:2].lower()
    text = msg.get("text","") or msg.get("caption","")
    if text:
        cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
        if len(text) > 0 and cyr/len(text) > 0.3: return "ru"
    return "en"


def _safe(text: str) -> str:
    return _KEY_RE.sub("[***]", str(text))


def _txt(inner) -> str:
    if isinstance(inner, str): return _safe(inner[:4000])
    if not isinstance(inner, dict): return _safe(str(inner)[:500])
    for k in ("answer","translated","post","transcription","text","content","output","message"):
        v = inner.get(k)
        if v and isinstance(v, str) and len(v.strip()) > 5:
            if len(v) == 36 and v.count("-") == 4: continue
            return _safe(v[:4000])
    if inner.get("output_path"):
        return f"✅ Файл сохранён:\n<code>{inner['output_path']}</code>"
    parts = [v for k,v in inner.items()
             if k not in _HIDDEN and isinstance(v,str) and 5<len(v)<500
             and not _KEY_RE.search(v)
             and not (len(v)==36 and v.count("-")==4)]
    return _safe(parts[0][:4000]) if parts else "✅ Готово."


async def handle_user_bot_update(token_hash: str, data: dict):
    try:
        async with get_session() as session:
            bot = (await session.execute(
                select(Bot).where(Bot.token_hash == token_hash, Bot.is_active == True)
            )).scalar_one_or_none()
            if not bot: logger.warning(f"No bot hash={token_hash}"); return
            raw = decrypt_token(bot.token_encrypted, settings.secret_key)
            utg = TelegramClient(raw)
            if msg := data.get("message"):
                await _process(utg, bot, msg, session)
            elif cb := data.get("callback_query"):
                await _handle_cb(utg, bot, cb, session)
    except Exception as e:
        logger.error(f"user_bot hash={token_hash}: {e}", exc_info=True)


async def _handle_cb(utg, bot, cb: dict, session):
    cid = cb["message"]["chat"]["id"]
    data = cb.get("data","")
    await utg.answer_callback_query(cb["id"])
    if not data.startswith("cloud_alt|"): return
    parts = data.split("|", 3)
    if len(parts) < 3: return
    orig_expert, orig_text, media_url = parts[1], parts[2], (parts[3] if len(parts) > 3 else "")

    alt = _CLOUD_ALT.get(orig_expert)
    if not alt:
        await utg.send_message(cid, "😔 Облачный аналог не найден.\n\nПодключите ПК: /connect")
        return

    # Get bot's keys (includes user-provided fal.ai key if any)
    bot_keys = get_bot_keys(bot, settings.secret_key)
    fal_key = bot_keys.get("fal_api_key","")

    if not fal_key:
        await utg.send_message(cid,
            "❌ Для этой функции нужен ключ fal.ai.\n\n"
            "Добавьте ключ командой /apikeys в боте @extnickbot_bot (конструктор)")
        return

    await utg.send_chat_action(cid, "upload_photo")
    await utg.send_message(cid, f"⏳ Запускаю <b>{alt}</b>...")
    params: dict = {}
    if media_url: params.update({"image_url": media_url, "audio_url": media_url, "file_url": media_url})
    if orig_text and orig_text not in list(_DEFAULT_INTENT.values()): params["prompt"] = orig_text
    params["fal_api_key"] = fal_key
    params["api_key"] = bot_keys.get("openai_api_key", settings.openai_api_key)
    result = await _run_cloud(alt, params)
    await _respond(utg, cid, result, False, alt)


async def _process(utg, bot, msg: dict, session):
    cid = msg["chat"]["id"]
    raw_text = msg.get("text","").strip()
    caption = msg.get("caption","").strip()
    mt="text"; fid=None
    if msg.get("photo"):    mt="photo";    fid=msg["photo"][-1]["file_id"]
    elif msg.get("video"):  mt="video";    fid=msg["video"]["file_id"]
    elif msg.get("voice"):  mt="voice";    fid=msg["voice"]["file_id"]
    elif msg.get("audio"):  mt="audio";    fid=msg["audio"]["file_id"]
    elif msg.get("document"): mt="document"; fid=msg["document"]["file_id"]
    text = caption or raw_text
    if not text and mt != "text": text = _DEFAULT_INTENT[mt]
    if not text: return
    lang = _detect_lang(msg)

    exps = (await session.execute(
        select(BotExpert).where(BotExpert.bot_id == bot.id, BotExpert.is_active == True)
        .order_by(BotExpert.sort_order))).scalars().all()

    if raw_text in ("/start","/help"):
        if exps:
            lines = "\n".join(
                f"{'☁️' if e.expert_name in CLOUD_RUNNERS else '💻'} {e.display_name or e.expert_name}"
                for e in exps)
            await utg.send_message(cid,
                f"👋 Extella AI\n\n<b>Функции ({len(exps)}):</b>\n{lines}\n\n"
                "☁️ = облако  💻 = ваш ПК\n\nПросто напишите что нужно!")
        else: await utg.send_message(cid, "👋 Бот настраивается.")
        return

    if not exps: await utg.send_message(cid, "Бот не настроен."); return

    furl = None
    if fid:
        furl = await utg.get_file_url(fid)
        if not furl: await utg.send_message(cid, "⚠️ Не удалось загрузить файл."); return

    await utg.send_chat_action(cid, _CHAT_ACTION.get(mt,"typing"))
    query = f"{text} {_MEDIA_HINT.get(mt,'')}".strip()
    best = await _route(exps, query)
    local = _is_local(best.expert_name, best.display_name or "")
    has_runner = best.expert_name in CLOUD_RUNNERS
    logger.info(f"bot={bot.id} expert={best.expert_name} local={local} cloud_runner={has_runner} lang={lang}")

    # Get this bot's keys (user-provided + platform fallback)
    bot_keys = get_bot_keys(bot, settings.secret_key)

    params = _build_params(best, text, mt, furl, lang, bot_keys)

    if has_runner:
        # Direct Railway execution
        result = await _run_cloud(best.expert_name, params)
    elif local:
        alt = _CLOUD_ALT.get(best.expert_name)
        if alt:
            alt_needs_keys = RUNNER_KEYS.get(alt, [])
            can_run_alt = all(bot_keys.get(k) for k in alt_needs_keys)
            if can_run_alt:
                # Run cloud alternative directly
                result = await _run_cloud(alt, params)
            else:
                # Offer choice: cloud (with key prompt) or connect device
                kb = {"inline_keyboard": [[
                    {"text": "☁️ Запустить в облаке",
                     "callback_data": f"cloud_alt|{best.expert_name}|{text[:60]}|{furl or ''}"},
                    {"text": "💻 Подключить ПК", "callback_data": "noop"},
                ]]}
                await utg.send_message(cid,
                    f"⚙️ <b>{best.display_name or best.expert_name}</b>\n\n"
                    "☁️ <b>Облако</b> — нужен ключ fal.ai (бесплатно)\n"
                    "💻 <b>Ваш ПК</b> — без ограничений (Extella Desktop)\n\n"
                    "Добавить ключ fal.ai: /apikeys в @extnickbot_bot",
                    reply_markup=kb)
                return
        else:
            if not bot.user_target_id:
                await utg.send_message(cid,
                    f"⚠️ <b>{best.display_name or best.expert_name}</b> работает только на вашем ПК.\n\n"
                    "Подключите Extella Desktop: /connect")
                return
            utok = decrypt_token(bot.user_extella_token_enc, settings.secret_key)
            client = ExtellaClient(utok)
            result = await client.run_expert(best.expert_name, params,
                                              timeout=90, target=bot.user_target_id)
    else:
        # Unknown — try Extella serverless
        result = await extella.run_expert(best.expert_name, params, timeout=90, target=None)

    await _respond(utg, cid, result, len(exps) > 1, best.expert_name)


def _build_params(best, text, mt, furl, lang, bot_keys: dict) -> dict:
    params = dict(best.params_json or {})
    pp = params.pop("__prompt_param__", "prompt")
    if furl:
        uk = {"photo":"image_url","video":"video_url",
              "voice":"audio_url","audio":"audio_url","document":"file_url"}.get(mt,"file_url")
        params[uk] = furl
        if text != _DEFAULT_INTENT.get(mt,"") and pp != uk: params[pp] = text
    else:
        params[pp] = text
    # Inject platform OpenAI key (never user key!)
    params["api_key"] = settings.openai_api_key
    # Inject user's fal.ai key only if they provided one
    fal = bot_keys.get("fal_api_key","")
    if fal:
        params["fal_api_key"] = fal
        params["fal_api_key_value"] = fal
    # Language
    inst = _LANG.get(lang, f"Respond in {lang} language.")
    if "system_prompt" in params:
        sp = params.get("system_prompt","")
        if inst not in sp: params["system_prompt"] = f"{sp}\n{inst}".strip()
    if "language" not in params: params["language"] = lang
    return params


async def _run_cloud(name: str, params: dict) -> dict:
    runner = CLOUD_RUNNERS.get(name)
    if not runner: return {"status":"error","message":f"No runner for {name}"}
    try: return await runner(**params)
    except Exception as e:
        logger.error(f"Runner {name}: {e}")
        return {"status":"error","message":str(e)}


async def _route(exps: list, query: str):
    if len(exps) == 1: return exps[0]
    try:
        ms = await extella.search_experts(query, limit=15)
        by = {e.expert_name: e for e in exps}
        for m in ms:
            if m["name"] in by:
                logger.info(f"Matched {m['name']} score={m.get('score','?')}")
                return by[m["name"]]
    except Exception as e: logger.warning(f"Route fail: {e}")
    return exps[0]


async def _respond(utg, cid: int, result: dict, multi: bool, name: str):
    label = f"🧠 <i>{name}</i>\n\n" if multi else ""
    if result.get("status") == "local_dispatched":
        await utg.send_message(cid, f"{label}💻 Задача на вашем ПК.\n📁 Результат → ~/Downloads")
        return
    if result.get("status") == "error":
        await utg.send_message(cid, f"⚠️ {_safe(result.get('message','Ошибка'))}"); return
    inner = result.get("result", result)
    if not inner: await utg.send_message(cid, label+"Нет ответа."); return
    if isinstance(inner,dict) and inner.get("status")=="error":
        await utg.send_message(cid, f"⚠️ {_safe(inner.get('message','Ошибка'))}"); return
    if isinstance(inner,dict):
        iu = (inner.get("result_url") or inner.get("image_url") or inner.get("output_url"))
        if iu: await _send_image(utg, cid, iu, label+inner.get("message","✅")); return
        au = inner.get("audio_url") or inner.get("voice_url") or inner.get("tts_url")
        if au:
            r = await utg.send_voice(cid, au)
            if not r.get("ok"): await utg.send_message(cid, f'{label}🎵 <a href="{au}">Аудио</a>')
            elif label: await utg.send_message(cid, label.strip())
            return
        vu = inner.get("video_url") or inner.get("output_video_url")
        if vu:
            r = await utg.send_video(cid, vu, caption=label+"✅")
            if not r.get("ok"): await utg.send_message(cid, f'{label}🎬 <a href="{vu}">Видео</a>')
            return
        if inner.get("output_path") and inner.get("status")=="success":
            await utg.send_message(cid, f"{label}✅ Файл:\n<code>{inner['output_path']}</code>"); return
    await utg.send_message(cid, label+_txt(inner))


async def _send_image(utg, cid: int, url: str, caption: str):
    import httpx
    size = 0
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            hr = await c.head(url)
            size = int(hr.headers.get("content-length",0))
    except Exception: pass
    if size > 20*1024*1024:
        await utg.send_message(cid, f"{caption}\n📎 Файл {size//1024//1024}МБ: <a href=\"{url}\">Скачать</a>")
        return
    r = await utg.send_photo(cid, url, caption=caption)
    if r.get("ok"): return
    r2 = await utg.send_document(cid, url, caption=caption)
    if r2.get("ok"): return
    await utg.send_message(cid, f'{caption}\n🖼 <a href="{url}">Открыть</a>')
