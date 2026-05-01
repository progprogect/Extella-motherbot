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
            bot = (await session.execute(
                select(Bot).where(Bot.token_hash == token_hash, Bot.is_active == True)
            )).scalar_one_or_none()
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
        select(BotExpert).where(
            BotExpert.bot_id == bot.id, BotExpert.is_active == True)
        .order_by(BotExpert.sort_order)
    )).scalars().all()

    if text == "/start":
        if experts:
            lines = "\n".join(f"\u2022 {e.display_name or e.expert_name}" for e in experts)
            await user_tg.send_message(
                chat_id,
                f"\U0001f44b Привет! Этот бот работает на базе <b>Extella AI</b>.\n\n"
                f"<b>Доступно функций: {len(experts)}</b>\n{lines}\n\n"
                "Просто напиши что нужно — я разберусь!"
            )
        else:
            await user_tg.send_message(chat_id, "\U0001f44b Привет! Бот настраивается.")
        return

    if text == "/help":
        if experts:
            lines = "\n".join(
                f"\u2022 <b>{e.expert_name}</b>: {(e.display_name or '')[:60]}"
                for e in experts)
            await user_tg.send_message(
                chat_id, f"<b>Доступные функции:</b>\n\n{lines}\n\nПросто опишите задачу!")
        return

    if not experts:
        await user_tg.send_message(chat_id, "Бот ещё не настроен. Обратитесь к администратору.")
        return

    await user_tg.send_chat_action(chat_id, "typing")

    # Smart routing: find best expert for this message
    best = await _route(experts, text)
    logger.info(f"Routing: bot={bot.id} expert={best.expert_name} msg={text[:40]!r}")

    params = dict(best.params_json or {})
    prompt_param = params.pop("__prompt_param__", "prompt")
    params[prompt_param] = text
    if settings.openai_api_key:
        params["api_key"] = settings.openai_api_key

    result = await extella.run_expert(expert_name=best.expert_name, params=params, timeout=60)
    response = _extract(result)

    # Show which expert answered (only if multiple)
    if len(experts) > 1:
        response = f"<i>\U0001f9e0 {best.expert_name}</i>\n\n{response}"

    await user_tg.send_message(chat_id, response)


async def _route(experts: list, message: str):
    """Semantic routing: search Extella with user message, match to configured experts"""
    if len(experts) == 1:
        return experts[0]
    try:
        matches = await extella.search_experts(message, limit=15)
        by_name = {e.expert_name: e for e in experts}
        for m in matches:
            if m["name"] in by_name:
                logger.info(f"Semantic match: {m['name']} score={m.get('score', '?')} for {message[:30]!r}")
                return by_name[m["name"]]
    except Exception as e:
        logger.error(f"Routing failed: {e}")
    return experts[0]


def _extract(result: dict) -> str:
    if result.get("status") == "error":
        return f"\u26a0\ufe0f Ошибка: {result.get('message', 'Unknown')}"
    inner = result.get("result", {})
    if not inner:
        return "Не получил ответ. Попробуй ещё раз."
    if isinstance(inner, str):
        return inner
    if isinstance(inner, dict):
        if inner.get("status") == "error":
            return f"\u26a0\ufe0f {inner.get('message', 'error')}"
        for key in ("answer", "translated", "post", "text", "content", "output", "message"):
            if key in inner and isinstance(inner[key], str):
                return inner[key]
        return str(inner)
    return str(inner)
