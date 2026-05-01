import logging
from sqlalchemy import select
from .database import Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import decrypt_token
from .config import settings

logger = logging.getLogger(__name__)
extella = ExtellaClient(settings.extella_token)

_DEFAULT_INTENT = {
    "photo":    "обработай это изображение",
    "video":    "опиши это видео",
    "voice":    "транскрибируй это голосовое сообщение",
    "audio":    "транскрибируй этот аудиофайл",
    "document": "обработай этот документ",
}
_CHAT_ACTION = {
    "text":     "typing",
    "photo":    "upload_photo",
    "video":    "upload_video",
    "voice":    "record_voice",
    "audio":    "upload_voice",
    "document": "upload_document",
}
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

    raw_text = msg.get("text",    "").strip()
    caption  = msg.get("caption", "").strip()

    media_type = "text"
    file_id    = None

    if msg.get("photo"):
        media_type = "photo"
        file_id    = msg["photo"][-1]["file_id"]
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

    text = caption or raw_text
    if not text and media_type != "text":
        text = _DEFAULT_INTENT[media_type]
    if not text:
        return

    experts = (await session.execute(
        select(BotExpert)
        .where(BotExpert.bot_id == bot.id, BotExpert.is_active == True)
        .order_by(BotExpert.sort_order)
    )).scalars().all()

    if raw_text in ("/start", "/help"):
        if experts:
            lines = "\n".join(f"\u2022 {e.display_name or e.expert_name}" for e in experts)
            caps = (
                "\n\U0001f4ac Текст \u2014 вопросы, переводы, посты"
                "\n\U0001f5bc\ufe0f Фото \u2014 обработка изображений"
                "\n\U0001f3a4 Голосовые \u2014 транскрипция речи"
                "\n\U0001f3b5 Аудио \u2014 транскрипция файлов"
                "\n\U0001f4c4 Документы \u2014 анализ и обработка"
            )
            await user_tg.send_message(
                chat_id,
                f"\U0001f44b Привет! Этот бот работает на базе <b>Extella AI</b>.\n\n"
                f"<b>\u0424\u0443\u043d\u043a\u0446\u0438\u0439: {len(experts)}</b>\n{lines}\n\n"
                f"<b>\u0423\u043c\u0435\u044e:</b>{caps}\n\n"
                "\u041f\u0440\u043e\u0441\u0442\u043e \u043e\u0442\u043f\u0440\u0430\u0432\u044c \u0447\u0442\u043e \u043d\u0443\u0436\u043d\u043e!"
            )
        else:
            await user_tg.send_message(chat_id, "\U0001f44b \u0411\u043e\u0442 \u043d\u0430\u0441\u0442\u0440\u0430\u0438\u0432\u0430\u0435\u0442\u0441\u044f.")
        return

    if not experts:
        await user_tg.send_message(chat_id, "\u0411\u043e\u0442 \u0435\u0449\u0451 \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d.")
        return

    file_url = None
    if file_id:
        file_url = await user_tg.get_file_url(file_id)
        if not file_url:
            await user_tg.send_message(chat_id, "\u26a0\ufe0f \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u0444\u0430\u0439\u043b.")
            return

    await user_tg.send_chat_action(chat_id, _CHAT_ACTION.get(media_type, "typing"))

    hint  = _MEDIA_SEARCH_HINT.get(media_type, "")
    query = f"{text} {hint}".strip()
    best  = await _route(experts, query)
    logger.info(f"bot={bot.id} expert={best.expert_name} media={media_type}")

    params       = dict(best.params_json or {})
    prompt_param = params.pop("__prompt_param__", "prompt")

    if file_url:
        url_key = {
            "photo":    "image_url",
            "video":    "video_url",
            "voice":    "audio_url",
            "audio":    "audio_url",
            "document": "file_url",
        }.get(media_type, "file_url")
        params[url_key] = file_url
        is_filler = text == _DEFAULT_INTENT.get(media_type, "")
        if not is_filler and prompt_param != url_key:
            params[prompt_param] = text
    else:
        params[prompt_param] = text

    if settings.openai_api_key:
        params["api_key"]      = settings.openai_api_key
    if settings.fal_api_key:
        params["fal_api_key"]       = settings.fal_api_key
        params["fal_api_key_value"] = settings.fal_api_key

    result = await extella.run_expert(
        expert_name=best.expert_name, params=params, timeout=90)
    await _respond(user_tg, chat_id, result, len(experts) > 1, best.expert_name)


async def _route(experts: list, query: str):
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
    label = f"\U0001f9e0 <i>{name}</i>\n\n" if multi else ""

    if result.get("status") == "error":
        await user_tg.send_message(chat_id, f"\u26a0\ufe0f {result.get('message','Unknown')}")
        return

    inner = result.get("result", result)
    if not inner:
        await user_tg.send_message(chat_id, label + "\u041d\u0435 \u043f\u043e\u043b\u0443\u0447\u0438\u043b \u043e\u0442\u0432\u0435\u0442.")
        return

    if isinstance(inner, dict) and inner.get("status") == "error":
        await user_tg.send_message(chat_id, f"\u26a0\ufe0f {inner.get('message','\u041e\u0448\u0438\u0431\u043a\u0430')}")
        return

    if isinstance(inner, dict):
        img_url = (inner.get("result_url") or inner.get("image_url")
                   or inner.get("output_url") or inner.get("output_image_url"))
        if img_url:
            cap = label + inner.get("message", "\u2705 \u0413\u043e\u0442\u043e\u0432\u043e!")
            r = await user_tg.send_photo(chat_id, img_url, caption=cap)
            if not r.get("ok"):
                await user_tg.send_message(
                    chat_id, f"{label}\u2705 \u0413\u043e\u0442\u043e\u0432\u043e!\n\U0001f517 <a href=\"{img_url}\">\u041e\u0442\u043a\u0440\u044b\u0442\u044c</a>")
            return

        aud = inner.get("audio_url") or inner.get("voice_url") or inner.get("tts_url")
        if aud:
            await user_tg.send_voice(chat_id, aud)
            if label: await user_tg.send_message(chat_id, label.strip())
            return

        vid = inner.get("video_url") or inner.get("output_video_url")
        if vid:
            r = await user_tg.send_video(chat_id, vid, caption=label + inner.get("message",""))
            if not r.get("ok"):
                await user_tg.send_message(chat_id, f"{label}<a href=\"{vid}\">\u041e\u0442\u043a\u0440\u044b\u0442\u044c</a>")
            return

    await user_tg.send_message(chat_id, label + _text(inner))


def _text(inner) -> str:
    if isinstance(inner, str): return inner[:4000]
    if isinstance(inner, dict):
        for k in ("answer","translated","post","transcription","text","content","output","message"):
            if k in inner and isinstance(inner[k], str) and inner[k]:
                return inner[k][:4000]
        return str(inner)[:2000]
    return str(inner)[:2000]
