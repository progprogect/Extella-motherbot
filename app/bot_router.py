import logging
from sqlalchemy import select
from .database import Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import decrypt_token
from .config import settings

logger = logging.getLogger(__name__)
extella = ExtellaClient(settings.extella_token)

# Default intent when user sends media without caption
_DEFAULT_INTENT = {
    "photo":    "обработай это изображение",
    "video":    "опиши это видео",
    "voice":    "транскрибируй это голосовое сообщение",
    "audio":    "транскрибируй этот аудиофайл",
    "document": "обработай этот документ",
}

# Telegram chat action per media type
_CHAT_ACTION = {
    "text":     "typing",
    "photo":    "upload_photo",
    "video":    "upload_video",
    "voice":    "record_voice",
    "audio":    "upload_voice",
    "document": "upload_document",
}

# Extella search enrichment keywords per media type
_MEDIA_SEARCH_HINT = {
    "photo":    "image photo visual processing",
    "video":    "video processing",
    "voice":    "voice audio transcription speech to text",
    "audio":    "audio transcription processing",
    "document": "document file text extraction",
}


async def handle_user_bot_update(token_hash: str, data: dict):
    try:
        async with get_session() as session:
            bot = (await session.execute(
                select(Bot).where(Bot.token_hash == token_hash, Bot.is_active == True)
            )).scalar_one_or_none()
            if not bot:
                logger.warning(f"No active bot for hash={token_hash}")
                return
            raw_token = decrypt_token(bot.token_encrypted, settings.secret_key)
            user_tg = TelegramClient(raw_token)
            if msg := data.get("message"):
                await _process(user_tg, bot, msg, session)
            elif cb := data.get("callback_query"):
                await user_tg.answer_callback_query(cb["id"])
    except Exception as e:
        logger.error(f"user_bot error hash={token_hash}: {e}", exc_info=True)


async def _process(user_tg, bot, msg: dict, session):
    chat_id = msg["chat"]["id"]

    # ── Extract text & media ──────────────────────────────────────────────
    raw_text  = msg.get("text",    "").strip()
    caption   = msg.get("caption", "").strip()

    media_type = "text"
    file_id    = None

    if msg.get("photo"):
        media_type = "photo"
        file_id    = msg["photo"][-1]["file_id"]   # largest resolution
    elif msg.get("video"):
        media_type = "video"
        file_id    = msg["video"]["file_id"]
    elif msg.get("voice"):
        media_type = "voice"
        file_id    = msg["voice"]["file_id"]
    elif msg.get("audio"):
        media_type = "audio"
        file_id    = msg["audio"]["file_id"]
    elif msg.get("document"):
        media_type = "document"
        file_id    = msg["document"]["file_id"]

    # Effective text: caption wins for media, raw_text for pure text
    text = caption or raw_text
    if not text and media_type != "text":
        text = _DEFAULT_INTENT[media_type]
    if not text:
        return  # empty message, ignore

    # ── Load active experts ───────────────────────────────────────────────
    experts = (await session.execute(
        select(BotExpert)
        .where(BotExpert.bot_id == bot.id, BotExpert.is_active == True)
        .order_by(BotExpert.sort_order)
    )).scalars().all()

    # ── System commands ───────────────────────────────────────────────────
    if raw_text in ("/start", "/help"):
        lines = "\n".join(f"\u2022 {e.display_name or e.expert_name}" for e in experts)
        capabilities = (
            "\n\u{1F4AC} Текст \u2014 вопросы, переводы, посты\n"
            "\U0001F5BC Фото \u2014 обработка изображений\n"
            "\U0001F3A4 Голосовые \u2014 транскрипция речи\n"
            "\U0001F3B5 Аудио \u2014 транскрипция файлов\n"
            "\U0001F4C4 Документы \u2014 анализ и обработка"
        )
        await user_tg.send_message(
            chat_id,
            f"\U0001F44B Привет! Этот бот работает на базе <b>Extella AI</b>.\n\n"
            f"<b>Функций: {len(experts)}</b>\n{lines}\n\n"
            f"<b>Умею обрабатывать:</b>{capabilities}\n\n"
            "Просто отправь что нужно!"
            if experts else
            "\U0001F44B Бот настраивается. Скоро всё заработает!"
        )
        return

    if not experts:
        await user_tg.send_message(chat_id, "Бот ещё не настроен. Обратитесь к администратору.")
        return

    # ── Get file URL ──────────────────────────────────────────────────────
    file_url = None
    if file_id:
        file_url = await user_tg.get_file_url(file_id)
        if not file_url:
            await user_tg.send_message(chat_id, "⚠️ Не удалось загрузить файл. Попробуй ещё раз.")
            return

    # ── Typing indicator ──────────────────────────────────────────────────
    await user_tg.send_chat_action(chat_id, _CHAT_ACTION.get(media_type, "typing"))

    # ── Route to best expert ──────────────────────────────────────────────
    hint   = _MEDIA_SEARCH_HINT.get(media_type, "")
    query  = f"{text} {hint}".strip()
    best   = await _route(experts, query)
    logger.info(f"bot={bot.id} expert={best.expert_name} media={media_type} q={query[:50]!r}")

    # ── Build params ──────────────────────────────────────────────────────
    params       = dict(best.params_json or {})
    prompt_param = params.pop("__prompt_param__", "prompt")

    if file_url:
        # Inject URL under the correct semantic key
        url_key = {
            "photo":    "image_url",
            "video":    "video_url",
            "voice":    "audio_url",
            "audio":    "audio_url",
            "document": "file_url",
        }.get(media_type, "file_url")

        params[url_key] = file_url

        # Inject user text if it's meaningful (not the default filler) and doesn't clash
        is_filler = text == _DEFAULT_INTENT.get(media_type, "")
        if not is_filler and prompt_param != url_key:
            params[prompt_param] = text
    else:
        params[prompt_param] = text

    # Inject all available API keys
    if settings.openai_api_key:
        params["api_key"] = settings.openai_api_key
    if settings.fal_api_key:
        params["fal_api_key"]       = settings.fal_api_key
        params["fal_api_key_value"] = settings.fal_api_key

    # ── Call expert ───────────────────────────────────────────────────────
    result = await extella.run_expert(
        expert_name=best.expert_name, params=params, timeout=90)

    # ── Send response ─────────────────────────────────────────────────────
    await _respond(user_tg, chat_id, result, len(experts) > 1, best.expert_name)


async def _route(experts: list, query: str):
    """Semantic routing via Extella search."""
    if len(experts) == 1:
        return experts[0]
    try:
        matches = await extella.search_experts(query, limit=15)
        by_name = {e.expert_name: e for e in experts}
        for m in matches:
            if m["name"] in by_name:
                logger.info(f"Matched expert={m['name']} score={m.get('score','?')}")
                return by_name[m["name"]]
    except Exception as e:
        logger.warning(f"Routing failed: {e}")
    return experts[0]


async def _respond(user_tg, chat_id: int, result: dict, multi: bool, name: str):
    """Smart response: photo/voice/video/text based on expert output."""
    label = f"\U0001F9E0 <i>{name}</i>\n\n" if multi else ""

    # Network-level error
    if result.get("status") == "error":
        await user_tg.send_message(chat_id, f"⚠️ {result.get('message','Unknown error')}")
        return

    # Extella wraps expert result in {"result": {...}}
    inner = result.get("result", result)

    if not inner:
        await user_tg.send_message(chat_id, label + "Не получил ответ. Попробуй ещё раз.")
        return

    # Expert-level error
    if isinstance(inner, dict) and inner.get("status") == "error":
        await user_tg.send_message(chat_id, f"⚠️ {inner.get('message','Ошибка')}")
        return

    if isinstance(inner, dict):
        # ── Image result ──────────────────────────────────────────────────
        img_url = (inner.get("result_url") or inner.get("image_url")
                   or inner.get("output_url") or inner.get("output_image_url"))
        if img_url:
            cap = label + inner.get("message", "✅ Готово!")
            r = await user_tg.send_photo(chat_id, img_url, caption=cap)
            if not r.get("ok"):
                # Telegram can't fetch URL → send as link
                await user_tg.send_message(
                    chat_id,
                    f"{label}✅ Готово!\n🔗 <a href=\"{img_url}\">Открыть изображение</a>"
                )
            return

        # ── Audio/voice result ────────────────────────────────────────────
        aud_url = inner.get("audio_url") or inner.get("voice_url") or inner.get("tts_url")
        if aud_url:
            await user_tg.send_voice(chat_id, aud_url)
            if label:
                await user_tg.send_message(chat_id, label.strip())
            return

        # ── Video result ──────────────────────────────────────────────────
        vid_url = inner.get("video_url") or inner.get("output_video_url")
        if vid_url:
            r = await user_tg.send_video(chat_id, vid_url,
                                          caption=label + inner.get("message",""))
            if not r.get("ok"):
                await user_tg.send_message(
                    chat_id, f"{label}✅ Видео готово!\n🔗 <a href=\"{vid_url}\">Открыть</a>")
            return

    # ── Text fallback ─────────────────────────────────────────────────────
    await user_tg.send_message(chat_id, label + _text(inner))


def _text(inner) -> str:
    if isinstance(inner, str):
        return inner[:4000]
    if isinstance(inner, dict):
        for k in ("answer", "translated", "post", "transcription",
                  "text", "content", "output", "message"):
            if k in inner and isinstance(inner[k], str) and inner[k]:
                return inner[k][:4000]
        return str(inner)[:2000]
    return str(inner)[:2000]
