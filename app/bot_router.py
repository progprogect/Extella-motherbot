import logging, re, json
from sqlalchemy import select
from .database import Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import decrypt_token
from .config import settings

logger = logging.getLogger(__name__)
extella = ExtellaClient(settings.extella_token)

# ── Safety regex ──────────────────────────────────────────────────────────────
_KEY_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35,}"
    r"|eyJ[A-Za-z0-9_.-]{30,}"
    r"|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    r"|aafd[A-Za-z0-9_-]{25,}|[A-Za-z0-9_-]{30,}:[A-Za-z0-9_-]{30,})"
)

# ── HARD-CODED local expert names (Pillow/CV/ffmpeg/no-api experts) ───────────
# These experts SAVE FILES to local disk — cannot run serverless
_KNOWN_LOCAL = {
    "image_enhance", "improve_photo_quality",
    "remove_background_local", "remove_bg_local",
    "video_enhance", "video_upscale",
    "text_to_speech", "voice_clone_tortoise",
    "transcribe_audio_file", "transcribe_audio",
    "pdf_edit", "edit_pdf", "merge_pdf", "split_pdf",
    "organize_files", "scan_folder",
    "convert_file", "file_converter",
    "save_presentation_pptx", "build_presentation",
    "get_clipboard", "clipboard_monitor",
}

# Cloud alternatives for known local experts
_CLOUD_ALTERNATIVES = {
    "image_enhance":        "mb_image_enhance_cloud",
    "improve_photo_quality":"mb_image_enhance_cloud",
    "remove_background_local": "mb_remove_background_cloud",
    "remove_bg_local":      "mb_remove_background_cloud",
    "transcribe_audio_file": "mb_transcribe_voice",
    "transcribe_audio":     "mb_transcribe_voice",
}

_LANG = {
    "ru": "Отвечай только на русском языке.",
    "en": "Respond only in English.",
    "de": "Antworte nur auf Deutsch.",
    "fr": "Reponds uniquement en francais.",
    "es": "Responde solo en espanol.",
    "uk": "Відповідай тільки українською.",
    "it": "Rispondi solo in italiano.",
    "pt": "Responda apenas em portugues.",
    "zh": "只用中文回答。", "ja": "日本語のみで回答してください。",
    "ko": "한국어로만 답변하세요.", "tr": "Sadece Turkce yanit ver.",
    "pl": "Odpowiadaj tylko po polsku.", "ar": "أجب باللغة العربية فقط.",
}
_DEFAULT_INTENT = {
    "photo": "обработай это изображение", "video": "опиши это видео",
    "voice": "транскрибируй голосовое", "audio": "транскрибируй аудиофайл",
    "document": "обработай документ",
}
_CHAT_ACTION = {
    "text": "typing", "photo": "upload_photo", "video": "upload_video",
    "voice": "record_voice", "audio": "upload_voice", "document": "upload_document",
}
_MEDIA_HINT = {
    "photo": "image photo visual processing enhance quality",
    "video": "video processing", "voice": "voice audio transcription speech",
    "audio": "audio transcription", "document": "document file text",
}
_HIDDEN = {"execution_log","task_id","Kwargs","kwargs","expert_name","api_key",
           "fal_api_key","fal_api_key_value","language","system_prompt",
           "__prompt_param__","status"}
_MAX_PHOTO_URL = 10 * 1024 * 1024  # 10MB threshold for URL vs file send


def _is_local(expert: "BotExpert") -> bool:
    """Expert requires local machine (filesystem access)."""
    if expert.exec_type == "local":
        return True
    name = expert.expert_name.lower()
    if name in _KNOWN_LOCAL:
        return True
    desc = (expert.display_name or "").lower()
    local_words = [
        "pillow", "opencv", "ffmpeg", "rembg", "ollama", "llama",
        "output_path", "saves to", "writes file", "local file",
        "no api key needed", "no api key required", "no api",
        "locally", "local machine", "subprocess", "sqlite",
    ]
    return any(w in name + " " + desc for w in local_words)


def _safe(text: str) -> str:
    return _KEY_RE.sub("[***]", text)


def _detect_lang(msg: dict) -> str:
    lang = (msg.get("from") or {}).get("language_code", "")
    if lang: return lang[:2].lower()
    text = msg.get("text","") or msg.get("caption","")
    if text:
        cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
        if len(text) > 0 and cyr/len(text) > 0.3: return "ru"
    return "en"


def _inject_lang(params: dict, lang: str) -> dict:
    inst = _LANG.get(lang, f"Respond in {lang} language.")
    if "system_prompt" in params:
        sp = params.get("system_prompt","")
        if inst not in sp:
            params["system_prompt"] = f"{sp}\n{inst}".strip()
    if "language" not in params:
        params["language"] = lang
    return params


def _txt(inner) -> str:
    if isinstance(inner, str):
        return _safe(inner[:4000])
    if not isinstance(inner, dict):
        return _safe(str(inner)[:500])
    for k in ("answer","translated","post","transcription",
              "text","content","output","message","result"):
        v = inner.get(k)
        if v and isinstance(v, str) and len(v.strip()) > 5:
            if len(v) == 36 and v.count("-") == 4: continue
            return _safe(v[:4000])
    if inner.get("output_path"):
        return f"✅ Готово!\n📁 Файл на вашем ПК:\n<code>{inner['output_path']}</code>"
    parts = []
    for k, v in inner.items():
        if k in _HIDDEN: continue
        if not isinstance(v, str) or len(v) < 3 or len(v) > 500: continue
        if _KEY_RE.search(v): continue
        if len(v) == 36 and v.count("-") == 4: continue
        parts.append(v)
    if parts: return _safe(parts[0][:4000])
    return "✅ Задача выполнена."


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
    cb_id = cb["id"]
    data = cb.get("data","")
    await utg.answer_callback_query(cb_id)
    if not data.startswith("cloud_alt|"):
        return
    # cloud_alt|expert_name|original_text|media_url
    parts = data.split("|", 3)
    if len(parts) < 3: return
    _, orig_expert, orig_text = parts[0], parts[1], parts[2]
    media_url = parts[3] if len(parts) > 3 else ""

    # Find cloud alternative
    alt_name = _CLOUD_ALTERNATIVES.get(orig_expert)
    if not alt_name:
        # Search Extella for cloud version
        alts = await extella.search_experts(f"cloud API {orig_text}", limit=5)
        for m in alts:
            if m["name"] != orig_expert and m["name"] not in _KNOWN_LOCAL:
                alt_name = m["name"]
                break

    if not alt_name:
        await utg.send_message(cid,
            "😔 Не нашёл облачного аналога.\n\n"
            "Подключи компьютер через /connect чтобы использовать эту функцию.")
        return

    await utg.send_chat_action(cid, "upload_photo")
    params = {}
    if media_url:
        params["image_url"] = media_url
        params["audio_url"] = media_url
        params["file_url"] = media_url
    if orig_text and orig_text not in list(_DEFAULT_INTENT.values()):
        params["prompt"] = orig_text
    if settings.openai_api_key: params["api_key"] = settings.openai_api_key
    if settings.fal_api_key:
        params["fal_api_key"] = settings.fal_api_key
        params["fal_api_key_value"] = settings.fal_api_key

    await utg.send_message(cid, f"☁️ Запускаю облачный аналог: <b>{alt_name}</b>...")
    result = await extella.run_expert(alt_name, params, timeout=120, target=None)
    await _respond(utg, cid, result, False, alt_name)


async def _process(utg, bot, msg: dict, session):
    cid = msg["chat"]["id"]
    raw_text = msg.get("text","").strip()
    caption = msg.get("caption","").strip()
    mt = "text"; fid = None
    if msg.get("photo"):    mt = "photo";    fid = msg["photo"][-1]["file_id"]
    elif msg.get("video"):  mt = "video";    fid = msg["video"]["file_id"]
    elif msg.get("voice"):  mt = "voice";    fid = msg["voice"]["file_id"]
    elif msg.get("audio"):  mt = "audio";    fid = msg["audio"]["file_id"]
    elif msg.get("document"): mt = "document"; fid = msg["document"]["file_id"]
    text = caption or raw_text
    if not text and mt != "text": text = _DEFAULT_INTENT[mt]
    if not text: return
    lang = _detect_lang(msg)

    exps = (await session.execute(
        select(BotExpert).where(BotExpert.bot_id == bot.id, BotExpert.is_active == True)
        .order_by(BotExpert.sort_order))).scalars().all()

    if raw_text in ("/start", "/help"):
        if exps:
            lines = "\n".join(
                f"{'☁️' if not _is_local(e) else '💻'} {e.display_name or e.expert_name}"
                for e in exps)
            local_n = sum(1 for e in exps if _is_local(e))
            conn = ""
            if local_n and not bot.user_target_id:
                conn = f"\n\n⚠️ {local_n} функц. работают только на вашем ПК. /connect"
            await utg.send_message(cid,
                f"👋 Привет! Extella AI | {len(exps)} функций\n{lines}{conn}\n\n"
                "Просто напишите что нужно!")
        else: await utg.send_message(cid, "👋 Бот настраивается.")
        return

    if not exps: await utg.send_message(cid, "Бот ещё не настроен."); return

    furl = None
    if fid:
        furl = await utg.get_file_url(fid)
        if not furl: await utg.send_message(cid, "⚠️ Не удалось загрузить файл."); return

    await utg.send_chat_action(cid, _CHAT_ACTION.get(mt,"typing"))
    query = f"{text} {_MEDIA_HINT.get(mt,'')}".strip()
    best = await _route(exps, query)
    logger.info(f"bot={bot.id} expert={best.expert_name} type={mt} lang={lang} local={_is_local(best)}")

    # ── LOCAL EXPERT handling ─────────────────────────────────────────────────
    if _is_local(best):
        if not bot.user_target_id or not bot.user_extella_token_enc:
            # Check if there's a cloud alternative
            alt = _CLOUD_ALTERNATIVES.get(best.expert_name)
            if alt:
                # Offer to use cloud alternative
                kb = {"inline_keyboard": [[
                    {"text": f"☁️ Запустить в облаке",
                     "callback_data": f"cloud_alt|{best.expert_name}|{text[:80]}|{furl or ''}"},
                    {"text": "💻 Подключить ПК",
                     "callback_data": "noop_connect"},
                ]]}
                await utg.send_message(cid,
                    f"⚙️ <b>{best.display_name or best.expert_name}</b>\n\n"
                    f"Эта функция доступна в двух режимах:\n\n"
                    f"☁️ <b>Облако</b> — работает прямо сейчас, результат в Telegram\n"
                    f"💻 <b>Ваш ПК</b> — быстрее, без ограничений (нужна Extella Desktop)",
                    reply_markup=kb)
            else:
                await utg.send_message(cid,
                    f"⚠️ <b>{best.display_name or best.expert_name}</b> работает только на вашем ПК.\n\n"
                    "Установите Extella Desktop и подключите компьютер командой /connect")
            return

        # Device is connected — run locally
        utok = decrypt_token(bot.user_extella_token_enc, settings.secret_key)
        client = ExtellaClient(utok)
        params = _build_params(best, text, mt, furl, lang)
        result = await client.run_expert(best.expert_name, params,
                                          timeout=90, target=bot.user_target_id)
    else:
        # CLOUD expert — always target=None (serverless)
        params = _build_params(best, text, mt, furl, lang)
        result = await extella.run_expert(best.expert_name, params,
                                           timeout=90, target=None)

    await _respond(utg, cid, result, len(exps) > 1, best.expert_name)


def _build_params(best: "BotExpert", text: str, mt: str, furl: str | None, lang: str) -> dict:
    params = dict(best.params_json or {})
    pp = params.pop("__prompt_param__", "prompt")
    if furl:
        uk = {"photo":"image_url","video":"video_url",
              "voice":"audio_url","audio":"audio_url","document":"file_url"}.get(mt,"file_url")
        params[uk] = furl
        if text != _DEFAULT_INTENT.get(mt,"") and pp != uk:
            params[pp] = text
    else:
        params[pp] = text
    if settings.openai_api_key: params["api_key"] = settings.openai_api_key
    if settings.fal_api_key:
        params["fal_api_key"] = settings.fal_api_key
        params["fal_api_key_value"] = settings.fal_api_key
    params = _inject_lang(params, lang)
    return params


async def _route(exps: list, query: str):
    if len(exps) == 1: return exps[0]
    try:
        ms = await extella.search_experts(query, limit=15)
        by = {e.expert_name: e for e in exps}
        for m in ms:
            if m["name"] in by:
                logger.info(f"Matched {m['name']} score={m.get('score','?')}")
                return by[m["name"]]
    except Exception as e:
        logger.warning(f"Route fail: {e}")
    return exps[0]


async def _respond(utg, cid: int, result: dict, multi: bool, name: str):
    label = f"🧠 <i>{name}</i>\n\n" if multi else ""

    # ── Local dispatched to user's Mac ────────────────────────────────────────
    if result.get("status") == "local_dispatched":
        await utg.send_message(cid,
            f"{label}💻 <b>Задача запущена на вашем компьютере</b>\n\n"
            f"Функция: <b>{result.get('expert_name','?')}</b>\n\n"
            "📁 Результат будет сохранён в папке <b>~/Downloads</b>\n"
            "<i>Убедитесь, что Extella Desktop запущен.</i>")
        return

    # ── Error ─────────────────────────────────────────────────────────────────
    if result.get("status") == "error":
        await utg.send_message(cid, f"⚠️ {_safe(result.get('message','Ошибка'))}")
        return

    inner = result.get("result", result)
    if not inner:
        await utg.send_message(cid, label + "Нет ответа. Попробуйте ещё раз."); return

    if isinstance(inner, dict) and inner.get("status") == "error":
        await utg.send_message(cid, f"⚠️ {_safe(inner.get('message','Ошибка'))}"); return

    if isinstance(inner, dict):
        # ── Image URL ─────────────────────────────────────────────────────────
        iu = (inner.get("result_url") or inner.get("image_url") or
              inner.get("output_url") or inner.get("output_image_url"))
        if iu:
            await _send_image(utg, cid, iu, label + inner.get("message","✅ Готово!"))
            return

        # ── Audio URL ─────────────────────────────────────────────────────────
        au = inner.get("audio_url") or inner.get("voice_url") or inner.get("tts_url")
        if au:
            r = await utg.send_voice(cid, au)
            if not r.get("ok"):
                await utg.send_message(cid, f'{label}🎵 <a href="{au}">Слушать аудио</a>')
            elif label:
                await utg.send_message(cid, label.strip())
            return

        # ── Video URL ─────────────────────────────────────────────────────────
        vu = inner.get("video_url") or inner.get("output_video_url")
        if vu:
            r = await utg.send_video(cid, vu, caption=label + "✅")
            if not r.get("ok"):
                await utg.send_message(cid, f'{label}🎬 <a href="{vu}">Смотреть видео</a>')
            return

        # ── Local output_path ─────────────────────────────────────────────────
        if inner.get("output_path") and inner.get("status") == "success":
            await utg.send_message(cid,
                f"{label}✅ Файл сохранён на вашем компьютере:\n"
                f"📁 <code>{inner['output_path']}</code>")
            return

    # ── Text fallback ─────────────────────────────────────────────────────────
    await utg.send_message(cid, label + _txt(inner))


async def _send_image(utg, cid: int, url: str, caption: str):
    """Send image — try as photo first, fall back to link for large files."""
    try:
        import httpx
        # HEAD request to check size
        async with httpx.AsyncClient(timeout=10) as c:
            hr = await c.head(url)
            size = int(hr.headers.get("content-length", 0))
    except Exception:
        size = 0

    if size > 20 * 1024 * 1024:
        # File too large for Telegram photo (>20MB)
        await utg.send_message(cid,
            f"{caption}\n\n📎 <b>Файл большой ({size//1024//1024}MB)</b> — "
            f'<a href="{url}">Открыть/скачать</a>')
        return

    # Try sending as photo
    r = await utg.send_photo(cid, url, caption=caption)
    if r.get("ok"):
        return

    # Photo failed → try as document
    r2 = await utg.send_document(cid, url, caption=caption)
    if r2.get("ok"):
        return

    # Last resort: link
    await utg.send_message(cid, f"{caption}\n\n🖼 <a href=\"{url}\">Открыть изображение</a>")
