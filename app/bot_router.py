"""
Motherbot v8 — Universal Dynamic Expert Execution

Execution modes (automatic, no hardcoded logic per expert):
  1. Serverless (EXTELLA_SERVERLESS_TOKEN, no target):
     wait=true → synchronous result → Railway → Telegram ✅
  2. User device (user_extella_token + user_target_id):
     wait=true, timeout=120 → if result → Telegram ✅
     If task_id (async) → inform user, check callback
  3. Async callback (expert POSTs to /expert_result endpoint):
     Expert receives __tg_bot_token__ + __tg_chat_id__ + __railway_callback_url__
     → can send result directly when done

Key design:
  - NO hardcoded cloud_runners — everything goes through Extella dynamically
  - ALL user API keys injected into every expert call
  - Expert picks whatever params it needs
  - Results always delivered to Telegram (text/photo/voice/video/document)
"""
import logging
import re
from sqlalchemy import select
from .database import Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import decrypt_token
from .config import settings
from .key_manager import build_expert_params

logger = logging.getLogger(__name__)
extella = ExtellaClient(settings.extella_token)

# ── Security ──────────────────────────────────────────────────────────────────
_KEY_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35,}"
    r"|eyJ[A-Za-z0-9_.-]{30,}|aafd[A-Za-z0-9_-]{25,}"
    r"|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"
)

_LANG = {
    "ru": "Отвечай только на русском языке.",
    "en": "Respond only in English.",
    "de": "Antworte nur auf Deutsch.",
    "fr": "Reponds uniquement en francais.",
    "es": "Responde solo en espanol.",
    "uk": "Відповідай тільки українською.",
    "it": "Rispondi solo in italiano.",
    "pt": "Responda apenas em portugues.",
    "zh": "只用中文回答。",
    "ja": "日本語のみで回答してください。",
    "ko": "한국어로만 답변하세요.",
    "tr": "Sadece Turkce yanit ver.",
    "pl": "Odpowiadaj tylko po polsku.",
    "ar": "أجب باللغة العربية فقط.",
}
_DEFAULT_INTENT = {
    "photo":    "обработай это изображение",
    "video":    "опиши это видео",
    "voice":    "транскрибируй голосовое сообщение",
    "audio":    "транскрибируй аудиофайл",
    "document": "обработай документ",
}
_CHAT_ACTION = {
    "text":     "typing",
    "photo":    "upload_photo",
    "video":    "upload_video",
    "voice":    "record_voice",
    "audio":    "upload_voice",
    "document": "upload_document",
}
_MEDIA_HINT = {
    "photo":    "image photo visual processing enhance quality",
    "video":    "video processing analyze describe",
    "voice":    "voice audio transcription speech to text whisper",
    "audio":    "audio transcription processing",
    "document": "document file text extraction analysis",
}
_HIDDEN = {
    "execution_log", "task_id", "Kwargs", "kwargs", "expert_name",
    "api_key", "openai_api_key", "fal_api_key", "fal_api_key_value",
    "anthropic_api_key", "replicate_api_token", "groq_api_key",
    "language", "system_prompt", "__prompt_param__",
    "__tg_bot_token__", "__tg_chat_id__", "__railway_callback_url__",
    "status",
}


def _safe(text: str) -> str:
    return _KEY_RE.sub("[***]", str(text))


def _detect_lang(msg: dict) -> str:
    lang = (msg.get("from") or {}).get("language_code", "")
    if lang:
        return lang[:2].lower()
    text = msg.get("text", "") or msg.get("caption", "")
    if text:
        cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
        if cyr / max(len(text), 1) > 0.3:
            return "ru"
    return "en"


def _extract_text(inner) -> str:
    if isinstance(inner, str):
        return _safe(inner[:4000])
    if not isinstance(inner, dict):
        return _safe(str(inner)[:500])
    for k in ("answer", "translated", "post", "transcription", "summary",
              "text", "content", "output", "message", "result", "data"):
        v = inner.get(k)
        if v and isinstance(v, str) and len(v.strip()) > 5:
            if len(v) == 36 and v.count("-") == 4:
                continue  # skip UUIDs
            return _safe(v[:4000])
    if inner.get("output_path") and inner.get("status") == "success":
        return f"✅ Файл сохранён:\n<code>{inner['output_path']}</code>"
    parts = [v for k, v in inner.items()
             if k not in _HIDDEN
             and isinstance(v, str) and 5 < len(v) < 500
             and not _KEY_RE.search(v)
             and not (len(v) == 36 and v.count("-") == 4)]
    return _safe(parts[0][:4000]) if parts else "✅ Готово."



# Local-only experts that require filesystem/Pillow/ffmpeg on a real device
_KNOWN_LOCAL_EXPERTS = {
    "image_enhance", "improve_photo_quality",
    "remove_background_local", "remove_bg_local",
    "video_enhance", "video_upscale", "text_to_speech",
    "transcribe_audio_file", "audio_to_text_free",
    "pdf_edit", "edit_pdf", "merge_pdf", "split_pdf",
    "organize_files", "file_organizer", "scan_folder",
    "convert_file", "file_converter", "save_presentation_pptx",
}

async def handle_user_bot_update(token_hash: str, data: dict):
    try:
        async with get_session() as session:
            bot = (await session.execute(
                select(Bot).where(
                    Bot.token_hash == token_hash,
                    Bot.is_active == True
                )
            )).scalar_one_or_none()
            if not bot:
                logger.warning(f"No active bot for hash={token_hash}")
                return
            raw = decrypt_token(bot.token_encrypted, settings.secret_key)
            utg = TelegramClient(raw)
            if msg := data.get("message"):
                await _process(utg, bot, msg, session)
            elif cb := data.get("callback_query"):
                await utg.answer_callback_query(cb["id"])
    except Exception as e:
        logger.error(f"user_bot hash={token_hash}: {e}", exc_info=True)


async def _process(utg, bot, msg: dict, session):
    cid = msg["chat"]["id"]
    raw_text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()

    mt = "text"
    fid = None
    if msg.get("photo"):
        mt = "photo"
        fid = msg["photo"][-1]["file_id"]
    elif msg.get("video"):
        mt = "video"
        fid = msg["video"]["file_id"]
    elif msg.get("voice"):
        mt = "voice"
        fid = msg["voice"]["file_id"]
    elif msg.get("audio"):
        mt = "audio"
        fid = msg["audio"]["file_id"]
    elif msg.get("document"):
        mt = "document"
        fid = msg["document"]["file_id"]

    text = caption or raw_text
    if not text and mt != "text":
        text = _DEFAULT_INTENT[mt]
    if not text:
        return

    lang = _detect_lang(msg)

    exps = (await session.execute(
        select(BotExpert)
        .where(BotExpert.bot_id == bot.id, BotExpert.is_active == True)
        .order_by(BotExpert.sort_order)
    )).scalars().all()

    if raw_text in ("/start", "/help"):
        if exps:
            lines = "\n".join(f"• {e.display_name or e.expert_name}" for e in exps)
            await utg.send_message(
                cid,
                f"👋 Работаю на базе <b>Extella AI</b>\n\n"
                f"<b>Функции ({len(exps)}):</b>\n{lines}\n\n"
                "Отправьте текст, фото, голосовое или файл!"
            )
        else:
            await utg.send_message(cid, "👋 Бот настраивается.")
        return

    if not exps:
        await utg.send_message(cid, "Бот ещё не настроен.")
        return

    furl = None
    if fid:
        furl = await utg.get_file_url(fid)
        if not furl:
            await utg.send_message(cid, "⚠️ Не удалось загрузить файл.")
            return

    await utg.send_chat_action(cid, _CHAT_ACTION.get(mt, "typing"))

    query = f"{text} {_MEDIA_HINT.get(mt, '')}".strip()
    best = await _route(exps, query)
    logger.info(f"bot={bot.id} expert={best.expert_name} mt={mt} lang={lang}")

    params = _build_params(bot, best, text, mt, furl, lang, cid)

    # ── Execute ───────────────────────────────────────────────────────────────
    # Determine if we have a valid device UUID (not "auto", not empty, proper UUID format)
    tid = (bot.user_target_id or "").strip()
    has_valid_device = (
        len(tid) == 36 and tid.count("-") == 4 and tid != "auto"
        and bot.user_extella_token_enc
    )

    # Check if this expert requires local machine (filesystem/Pillow/ffmpeg etc.)
    is_local_expert = best.expert_name.lower() in _KNOWN_LOCAL_EXPERTS
    if not is_local_expert:
        desc = (best.display_name or "").lower()
        name_l = best.expert_name.lower()
        is_local_expert = any(w in name_l + " " + desc for w in [
            "pillow", "opencv", "ffmpeg", "rembg", "ollama",
            "output_path", "saves to", "local file",
            "no api key needed", "subprocess", "filesystem",
        ])

    if is_local_expert and not has_valid_device:
        # Local expert but no valid device UUID — ask user to provide it
        await utg.send_message(
            cid,
            "\u26a0\ufe0f <b>" + (best.display_name or best.expert_name) + "</b> "
            "requires your computer to run.\n\n"
            "To connect your device:\n"
            "1. Open <b>Extella Desktop</b>\n"
            "2. Find your <b>Device UUID</b> in Settings\n"
            "3. Use /connect in @extnickbot_bot and follow instructions\n\n"
            "<i>Or ask Extella AI agent: "
            "<code>What is my device UUID?</code></i>"
        )
        return

    if has_valid_device:
        # PLATFORM TOKEN + user device UUID → runs on user's machine
        # Platform token can access platform experts on any registered device
        result = await extella.run_expert(
            best.expert_name, params, wait=True, timeout=120,
            target=tid,  # explicit valid UUID
        )
        if result.get("status") == "async":
            logger.info(f"Device async for {best.expert_name}, noting for user")
            # Result will be saved to ~/Downloads on user's machine
    else:
        # Serverless — no target (platform routes to remote workers)
        result = await extella.run_expert(
            best.expert_name, params, wait=True, timeout=90)

    await _respond(utg, cid, result, len(exps) > 1, best.expert_name)


def _build_params(bot, best, text: str, mt: str, furl: str | None,
                  lang: str, chat_id: int) -> dict:
    """Build params dict — injects ALL available keys + media + language."""
    params = dict(best.params_json or {})
    pp = params.pop("__prompt_param__", "prompt")

    # Media
    if furl:
        uk = {
            "photo":    "image_url",
            "video":    "video_url",
            "voice":    "audio_url",
            "audio":    "audio_url",
            "document": "file_url",
        }.get(mt, "file_url")
        params[uk] = furl
        is_filler = text == _DEFAULT_INTENT.get(mt, "")
        if not is_filler and pp != uk:
            params[pp] = text
    else:
        params[pp] = text

    # Inject ALL API keys (platform + user-provided)
    all_keys = build_expert_params(bot, settings.secret_key, settings.openai_api_key)
    params.update(all_keys)

    # Language
    inst = _LANG.get(lang, f"Respond in {lang} language.")
    if "system_prompt" in params:
        sp = params.get("system_prompt", "")
        if inst not in sp:
            params["system_prompt"] = f"{sp}\n{inst}".strip()
    if "language" not in params:
        params["language"] = lang

    # Callback info — experts that support async result delivery can use this
    raw_bot_token = decrypt_token(bot.token_encrypted, settings.secret_key)
    params["__tg_bot_token__"] = raw_bot_token
    params["__tg_chat_id__"] = str(chat_id)
    if settings.railway_url:
        params["__railway_callback_url__"] = (
            f"{settings.railway_url}/expert_result/{bot.token_hash}/{chat_id}"
        )

    return params


async def _route(exps: list, query: str):
    if len(exps) == 1:
        return exps[0]
    try:
        ms = await extella.search_experts(query, limit=15)
        by = {e.expert_name: e for e in exps}
        for m in ms:
            name = m["name"]
            # Exact match
            if name in by:
                logger.info("Matched %s exact score=%s q=%s", name, m.get("score","?"), query[:35])
                return by[name]
            # Fuzzy: split library name into words, find bot expert containing same word
            parts = [part for part in name.split("_") if len(part) >= 4]
            for part in parts:
                for bot_name, bot_exp in by.items():
                    if part in bot_name:
                        logger.info("Matched %s fuzzy via %s/%s q=%s", bot_name, name, part, query[:35])
                        return bot_exp
    except Exception as e:
        logger.warning("Route fail: %s", e)
    return exps[0]

async def _respond(utg, cid: int, result: dict, multi: bool, name: str):
    label = f"🧠 <i>{name}</i>\n\n" if multi else ""

    # Async — expert dispatched to device
    if result.get("status") == "async":
        await utg.send_message(
            cid,
            f"{label}⏳ <b>Задача запущена на вашем устройстве</b>\n\n"
            "Эксперт работает локально. Результат появится в ~/Downloads.\n"
            "<i>Убедитесь, что Extella Desktop запущен.</i>",
        )
        return

    if result.get("status") == "error":
        await utg.send_message(cid, f"⚠️ {_safe(result.get('message', 'Ошибка'))}")
        return

    inner = result.get("result", result)
    if not inner:
        await utg.send_message(cid, label + "Нет ответа. Попробуйте ещё раз.")
        return

    if isinstance(inner, dict) and inner.get("status") == "error":
        await utg.send_message(cid, f"⚠️ {_safe(inner.get('message', 'Ошибка'))}")
        return

    if isinstance(inner, dict):
        # Expert already sent result to Telegram directly (local expert pattern)
        if inner.get("sent_to_telegram"):
            # Expert handled delivery itself — don't double-send
            return
        # Image
        iu = (inner.get("result_url") or inner.get("image_url")
              or inner.get("output_url") or inner.get("output_image_url"))
        if iu:
            await _send_media(utg, cid, iu,
                              label + inner.get("message", "✅"), "photo")
            return
        # Audio
        au = (inner.get("audio_url") or inner.get("voice_url")
              or inner.get("tts_url"))
        if au:
            await _send_media(utg, cid, au, label, "voice")
            return
        # Video
        vu = inner.get("video_url") or inner.get("output_video_url")
        if vu:
            await _send_media(utg, cid, vu, label + "✅", "video")
            return
        # Local file path
        if inner.get("output_path") and inner.get("status") == "success":
            await utg.send_message(
                cid,
                f"{label}✅ Файл сохранён на вашем устройстве:\n"
                f"📁 <code>{inner['output_path']}</code>",
            )
            return

    await utg.send_message(cid, label + _extract_text(inner))


async def _send_media(utg, cid: int, url: str, caption: str, media_type: str):
    """Send media to Telegram. Tries direct URL, falls back to document/link."""
    import httpx
    size = 0
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            hr = await c.head(url, follow_redirects=True)
            size = int(hr.headers.get("content-length", 0))
    except Exception:
        pass
    size_mb = size / 1024 / 1024

    if media_type == "photo":
        if size_mb > 50:
            await utg.send_message(
                cid,
                f"{caption}\n📎 Файл {size_mb:.1f}МБ — "
                f'<a href="{url}">Скачать</a>')
            return
        r = await utg.send_photo(cid, url, caption=caption)
        if r.get("ok"):
            return
        r2 = await utg.send_document(cid, url, caption=caption)
        if not r2.get("ok"):
            await utg.send_message(cid, f'{caption}\n🖼 <a href="{url}">Открыть</a>')

    elif media_type == "voice":
        if size_mb > 50:
            await utg.send_message(cid, f'🎵 <a href="{url}">Аудио</a>')
            return
        r = await utg.send_voice(cid, url)
        if not r.get("ok"):
            r2 = await utg.send_audio(cid, url, caption=caption)
            if not r2.get("ok"):
                await utg.send_message(cid, f'🎵 <a href="{url}">Аудио</a>')
        elif caption.strip():
            await utg.send_message(cid, caption.strip())

    elif media_type == "video":
        if size_mb > 50:
            await utg.send_message(cid, f'{caption}\n🎬 <a href="{url}">Смотреть</a>')
            return
        r = await utg.send_video(cid, url, caption=caption)
        if not r.get("ok"):
            await utg.send_message(cid, f'{caption}\n🎬 <a href="{url}">Видео</a>')
