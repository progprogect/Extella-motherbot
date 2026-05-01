import logging
from sqlalchemy import select
from .database import Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import decrypt_token
from .config import settings

logger = logging.getLogger(__name__)
extella = ExtellaClient(settings.extella_token)

async def handle_user_bot_update(token_hash: str, data: dict):
    try:
        async with get_session() as session:
            bot = (await session.execute(select(Bot).where(Bot.token_hash == token_hash, Bot.is_active == True))).scalar_one_or_none()
            if not bot:
                logger.warning(f"No active bot for hash={token_hash}")
                return
            raw_token = decrypt_token(bot.token_encrypted, settings.secret_key)
            user_tg = TelegramClient(raw_token)
            if msg := data.get("message"):
                await _process_message(user_tg, bot, msg, session)
            elif cb := data.get("callback_query"):
                await user_tg.answer_callback_query(cb["id"])
    except Exception as e:
        logger.error(f"user_bot error (hash={token_hash}): {e}", exc_info=True)

async def _process_message(user_tg, bot, message: dict, session):
    chat_id = message["chat"]["id"]
    text: str = message.get("text", "").strip()
    if not text:
        return
    experts = (await session.execute(
        select(BotExpert).where(BotExpert.bot_id == bot.id, BotExpert.is_active == True).order_by(BotExpert.sort_order)
    )).scalars().all()

    if text == "/start":
        feat_list = "\n".join(f"• {e.display_name or e.expert_name}" for e in experts)
        await user_tg.send_message(chat_id, f"👋 Привет! Я умею:\n\n{feat_list}\n\nПросто напиши что нужно!")
        return
    if text == "/help":
        feat_list = "\n".join(f"• {e.display_name or e.expert_name}" for e in experts)
        await user_tg.send_message(chat_id, f"Доступные функции:\n{feat_list}")
        return
    if not experts:
        await user_tg.send_message(chat_id, "Бот ещё не настроен.")
        return

    await user_tg.send_chat_action(chat_id, "typing")
    expert = experts[0]
    params = dict(expert.params_json or {})
    prompt_param = params.pop("__prompt_param__", "prompt")
    params[prompt_param] = text
    if settings.openai_api_key:
        params["api_key"] = settings.openai_api_key

    result = await extella.run_expert(expert_name=expert.expert_name, params=params, timeout=60)
    await user_tg.send_message(chat_id, _extract_response(result))

def _extract_response(result: dict) -> str:
    if result.get("status") == "error":
        return f"⚠️ Ошибка: {result.get('message', 'Unknown')}"
    inner = result.get("result", {})
    if not inner: return "Не получил ответ. Попробуй ещё раз."
    if isinstance(inner, str): return inner
    if isinstance(inner, dict):
        if inner.get("status") == "error": return f"⚠️ {inner.get('message', 'error')}"
        for key in ("answer", "translated", "post", "text", "content", "output", "message"):
            if key in inner and isinstance(inner[key], str): return inner[key]
        return str(inner)
    return str(inner)
