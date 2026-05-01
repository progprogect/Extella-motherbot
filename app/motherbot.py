import re, logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .database import User, Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .crypto import encrypt_token, token_to_hash, decrypt_token
from .config import settings
from .features import FEATURES

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"^\d{8,12}:[A-Za-z0-9_-]{35,}$")
motherbot = TelegramClient(settings.motherbot_token)

async def _get_or_create_user(session, telegram_id, username, first_name):
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(telegram_id=telegram_id, username=username, first_name=first_name, state="start")
        session.add(user)
        await session.flush()
    return user

def _build_feature_keyboard(selected_keys: set, bot_id: int) -> dict:
    rows = []
    for key, feat in FEATURES.items():
        icon = "✅ " if key in selected_keys else "➕ "
        rows.append([{"text": icon + feat["name"], "callback_data": f"feat|{key}|{bot_id}"}])
    if selected_keys:
        rows.append([{"text": "🚀 Активировать бота!", "callback_data": f"activate|{bot_id}"}])
    else:
        rows.append([{"text": "☝️ Выберите минимум 1 функцию", "callback_data": "noop"}])
    return {"inline_keyboard": rows}

async def handle_motherbot_update(data: dict):
    try:
        if msg := data.get("message"): await _handle_message(msg)
        elif cb := data.get("callback_query"): await _handle_callback(cb)
    except Exception as e:
        logger.error(f"motherbot error: {e}", exc_info=True)

async def _handle_message(message: dict):
    chat_id = message["chat"]["id"]
    text: str = message.get("text", "").strip()
    from_user = message["from"]
    tg_id = from_user["id"]
    async with get_session() as session:
        user = await _get_or_create_user(session, tg_id, from_user.get("username"), from_user.get("first_name"))
        if text in ("/start", "/help"): await _cmd_start(chat_id, user, session)
        elif text == "/mybots": await _cmd_mybots(chat_id, user, session)
        elif text == "/cancel":
            user.state = "start"; user.pending_bot_id = None; await session.flush()
            await motherbot.send_message(chat_id, "Отменено. /start — начать заново.")
        elif user.state == "waiting_token": await _handle_token_input(chat_id, text, user, session)
        else: await motherbot.send_message(chat_id, "Используй /start или /mybots")

async def _cmd_start(chat_id, user, session):
    user.state = "waiting_token"; await session.flush()
    await motherbot.send_message(chat_id,
        "👋 Привет! Я <b>Extella Motherbot</b> — конструктор умных Telegram-ботов.\n\n"
        "🤖 AI-ассистент (GPT-4)\n🌐 Переводчик\n✍️ Генератор контента\n\n"
        "──────────────────────\n"
        "<b>Шаг 1</b> — создай бота у @BotFather (/newbot)\n"
        "<b>Шаг 2</b> — скопируй токен и пришли сюда\n"
        "<b>Шаг 3</b> — выбери функции и активируй\n"
        "──────────────────────\n\n📋 <b>Пришли токен своего бота:</b>")

async def _handle_token_input(chat_id, text, user, session):
    if not TOKEN_RE.match(text):
        await motherbot.send_message(chat_id, "❌ Не похоже на токен.\nФормат: <code>1234567890:AABBcc...</code>\nПолучи у @BotFather")
        return
    await motherbot.send_message(chat_id, "⏳ Проверяю токен...")
    temp_bot = TelegramClient(text)
    result = await temp_bot.get_me()
    if not result.get("ok"):
        await motherbot.send_message(chat_id, f"❌ Токен недействителен.\n<code>{result.get('description', 'Unknown')}</code>")
        return
    bot_info = result["result"]
    t_hash = token_to_hash(text)
    dup = (await session.execute(select(Bot).where(Bot.token_hash == t_hash))).scalar_one_or_none()
    if dup:
        await motherbot.send_message(chat_id, f"⚠️ Бот @{bot_info['username']} уже зарегистрирован. /mybots")
        return
    bot = Bot(user_telegram_id=user.telegram_id, token_encrypted=encrypt_token(text, settings.secret_key),
              token_hash=t_hash, bot_telegram_id=bot_info["id"], bot_name=bot_info["first_name"],
              bot_username=bot_info.get("username"), is_active=False)
    session.add(bot); await session.flush()
    user.state = "choosing_features"; user.pending_bot_id = bot.id; await session.flush()
    await motherbot.send_message(chat_id,
        f"✅ Бот <b>{bot_info['first_name']}</b> (@{bot_info.get('username')}) подтверждён!\n\nВыбери функции 👇",
        reply_markup=_build_feature_keyboard(set(), bot.id))

async def _cmd_mybots(chat_id, user, session):
    bots = (await session.execute(select(Bot).where(Bot.user_telegram_id == user.telegram_id))).scalars().all()
    if not bots:
        await motherbot.send_message(chat_id, "У тебя пока нет ботов. /start"); return
    rows = [[{"text": f"{"✅" if b.is_active else "⏸"} @{b.bot_username or "?"} — {b.bot_name or "?"}", "callback_data": f"manage|{b.id}"}] for b in bots]
    rows.append([{"text": "➕ Добавить нового бота", "callback_data": "newbot"}])
    await motherbot.send_message(chat_id, f"🤖 Твои боты ({len(bots)}):", reply_markup={"inline_keyboard": rows})

async def _handle_callback(callback: dict):
    cb_id = callback["id"]; chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]; data = callback.get("data", "")
    from_user = callback["from"]; tg_id = from_user["id"]
    if data == "noop":
        await motherbot.answer_callback_query(cb_id, "Сначала выберите функцию!"); return
    if data == "newbot":
        async with get_session() as session:
            user = await _get_or_create_user(session, tg_id, from_user.get("username"), from_user.get("first_name"))
            await _cmd_start(chat_id, user, session)
        await motherbot.answer_callback_query(cb_id); return
    parts = data.split("|"); action = parts[0] if parts else ""
    async with get_session() as session:
        user = await _get_or_create_user(session, tg_id, from_user.get("username"), from_user.get("first_name"))
        if action == "feat" and len(parts) == 3:
            feature_key, bot_id = parts[1], int(parts[2])
            bot = (await session.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tg_id:
                await motherbot.answer_callback_query(cb_id, "Бот не найден"); return
            feat_cfg = FEATURES.get(feature_key)
            if not feat_cfg:
                await motherbot.answer_callback_query(cb_id, "Неизвестная функция"); return
            expert_name = feat_cfg["expert"]
            existing = (await session.execute(select(BotExpert).where(BotExpert.bot_id == bot_id, BotExpert.expert_name == expert_name))).scalar_one_or_none()
            if existing:
                await session.delete(existing)
                await motherbot.answer_callback_query(cb_id, f"Убрано: {feat_cfg['name']}")
            else:
                session.add(BotExpert(bot_id=bot_id, expert_name=expert_name, display_name=feat_cfg["name"], params_json=feat_cfg["params"]))
                await motherbot.answer_callback_query(cb_id, f"Добавлено: {feat_cfg['name']}")
            await session.flush()
            all_exp = (await session.execute(select(BotExpert).where(BotExpert.bot_id == bot_id))).scalars().all()
            selected = {fk for fk, fv in FEATURES.items() if any(e.expert_name == fv["expert"] for e in all_exp)}
            await motherbot.edit_message_text(chat_id, message_id, f"Выбрано: <b>{len(selected)}</b>\nНажми для переключения 👇", reply_markup=_build_feature_keyboard(selected, bot_id))
        elif action == "activate" and len(parts) == 2:
            bot_id = int(parts[1])
            bot = (await session.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tg_id:
                await motherbot.answer_callback_query(cb_id, "Бот не найден"); return
            experts = (await session.execute(select(BotExpert).where(BotExpert.bot_id == bot_id))).scalars().all()
            if not experts:
                await motherbot.answer_callback_query(cb_id, "Выберите хотя бы одну функцию!", show_alert=True); return
            await motherbot.answer_callback_query(cb_id, "⏳ Активирую...")
            raw_token = decrypt_token(bot.token_encrypted, settings.secret_key)
            webhook_url = f"{settings.railway_url}/bot/{bot.token_hash}/webhook"
            wh_result = await TelegramClient(raw_token).set_webhook(webhook_url)
            if not wh_result.get("ok"):
                await motherbot.send_message(chat_id, f"❌ Webhook error: {wh_result.get('description', '?')}"); return
            bot.webhook_url = webhook_url; bot.is_active = True
            user.state = "active"; user.pending_bot_id = None; await session.flush()
            feat_list = "\n".join(f"• {e.display_name or e.expert_name}" for e in experts)
            await motherbot.edit_message_text(chat_id, message_id,
                f"🎉 <b>Бот @{bot.bot_username} активирован!</b>\n\n<b>Функции:</b>\n{feat_list}\n\nПишите @{bot.bot_username}! Управление: /mybots")
        elif action == "manage" and len(parts) == 2:
            bot_id = int(parts[1])
            bot = (await session.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
            if not bot: await motherbot.answer_callback_query(cb_id, "Не найден"); return
            experts = (await session.execute(select(BotExpert).where(BotExpert.bot_id == bot_id))).scalars().all()
            feat_list = "\n".join(f"• {e.display_name or e.expert_name}" for e in experts) or "—"
            await motherbot.answer_callback_query(cb_id)
            await motherbot.send_message(chat_id,
                f"🤖 <b>@{bot.bot_username}</b>\nСтатус: {"✅ Активен" if bot.is_active else "⏸ Не активен"}\n\n<b>Функции:</b>\n{feat_list}",
                reply_markup={"inline_keyboard": [[{"text": "🗑 Деактивировать", "callback_data": f"deactivate|{bot_id}"}]]})
        elif action == "deactivate" and len(parts) == 2:
            bot_id = int(parts[1])
            bot = (await session.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
            if bot and bot.user_telegram_id == tg_id:
                raw_token = decrypt_token(bot.token_encrypted, settings.secret_key)
                await TelegramClient(raw_token).delete_webhook()
                bot.is_active = False; await session.flush()
                await motherbot.answer_callback_query(cb_id, "Деактивирован")
                await motherbot.send_message(chat_id, f"⏸ @{bot.bot_username} деактивирован.")
            else: await motherbot.answer_callback_query(cb_id, "Не найден")
        else: await motherbot.answer_callback_query(cb_id)
