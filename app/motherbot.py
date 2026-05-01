import re
import logging
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from .database import User, Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .crypto import encrypt_token, token_to_hash, decrypt_token
from .config import settings
from .extella_client import ExtellaClient

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"^\d{8,12}:[A-Za-z0-9_-]{35,}$")
motherbot = TelegramClient(settings.motherbot_token)
extella = ExtellaClient(settings.extella_token)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_desc(desc: str) -> str:
    """Strip long Parameters: section from expert description"""
    for sep in [". Parameters:", "\nParameters:", " Parameters:"]:
        if sep in desc:
            desc = desc.split(sep)[0]
            break
    return desc.strip()[:120]


def _detect_prompt_param(name: str, desc: str) -> str:
    n, d = name.lower(), desc.lower()
    if any(k in n for k in ("translat", "translate")):
        return "text"
    if any(k in n for k in ("image", "photo", "picture", "remove_bg")):
        return "image_url"
    if "input text" in d or "user text" in d:
        return "text"
    return "prompt"


def _build_expert_keyboard(all_exps: list[dict], selected: set[str], bot_id: int) -> dict:
    rows = []
    for exp in all_exps:
        name = exp["name"]
        label = _clean_desc(exp.get("description", name))
        label = label[:38] + "..." if len(label) > 38 else label
        icon = "✅" if name in selected else "◻️"
        rows.append([{"text": f"{icon} {label}", "callback_data": f"exp|{name}|{bot_id}"}])
    if selected:
        rows.append([{"text": "🚀 Активировать бота!", "callback_data": f"activate|{bot_id}"}])
    else:
        rows.append([{"text": "☝️ Выберите хотя бы 1 функцию", "callback_data": "noop"}])
    rows.append([{"text": "🔄 Описать заново", "callback_data": f"research|{bot_id}"}])
    return {"inline_keyboard": rows}


def _usage_examples(experts) -> str:
    lines = []
    for e in experts[:3]:
        n = e.expert_name.lower()
        if "translat" in n:
            lines.append("• <i>«Переведи на английский: Добрый день»</i>")
        elif "assistant" in n or "chat" in n or "openai" in n or "gpt" in n:
            lines.append("• <i>«Объясни что такое нейронные сети»</i>")
        elif "content" in n or "post" in n or "social" in n or "generat" in n:
            lines.append("• <i>«Напиши пост про запуск нового продукта»</i>")
        elif "email" in n or "draft" in n or "letter" in n:
            lines.append("• <i>«Напиши письмо клиенту о задержке»</i>")
        elif "summar" in n:
            lines.append("• <i>«Сделай краткое резюме этого текста: ...»</i>")
        elif "image" in n or "photo" in n or "background" in n:
            lines.append("• <i>Отправьте фото для обработки</i>")
        else:
            desc = (e.display_name or e.expert_name)[:40]
            lines.append(f"• <i>Напишите задачу для: {desc}</i>")
    return "\n".join(lines) if lines else "• <i>Просто напишите что нужно сделать</i>"


async def _get_or_create_user(session, telegram_id, username, first_name):
    r = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = r.scalar_one_or_none()
    if not user:
        user = User(telegram_id=telegram_id, username=username,
                    first_name=first_name, state="start")
        session.add(user)
        await session.flush()
    return user


# ── Entry point ───────────────────────────────────────────────────────────────

async def handle_motherbot_update(data: dict):
    try:
        if msg := data.get("message"):
            await _handle_message(msg)
        elif cb := data.get("callback_query"):
            await _handle_callback(cb)
    except Exception as e:
        logger.error(f"motherbot error: {e}", exc_info=True)


# ── Message handler ───────────────────────────────────────────────────────────

async def _handle_message(message: dict):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    from_user = message["from"]
    tg_id = from_user["id"]

    async with get_session() as session:
        user = await _get_or_create_user(
            session, tg_id, from_user.get("username"), from_user.get("first_name"))

        if text in ("/start", "/help"):
            await _cmd_start(chat_id, user, session)
        elif text == "/mybots":
            await _cmd_mybots(chat_id, user, session)
        elif text == "/cancel":
            user.state = "start"; user.pending_bot_id = None
            await session.flush()
            await motherbot.send_message(chat_id, "Отменено. /start — начать заново.")
        elif user.state == "waiting_token":
            await _handle_token_input(chat_id, text, user, session)
        elif user.state == "waiting_feature_description":
            await _handle_feature_description(chat_id, text, user, session)
        else:
            await motherbot.send_message(chat_id, "Используй /start или /mybots")


async def _cmd_start(chat_id, user, session):
    user.state = "waiting_token"; user.pending_bot_id = None
    await session.flush()
    await motherbot.send_message(
        chat_id,
        "👋 Привет! Я <b>Extella Motherbot</b> — конструктор умных Telegram-ботов.\n\n"
        "Я подберу AI-функции из библиотеки <b>Extella</b> специально под твой запрос "
        "и подключу их к твоему боту автоматически.\n\n"
        "──────────────────────\n"
        "<b>Шаг 1</b> — создай бота у @BotFather (/newbot)\n"
        "<b>Шаг 2</b> — пришли мне токен\n"
        "<b>Шаг 3</b> — опиши что должен уметь бот\n"
        "<b>Шаг 4</b> — готово, бот работает!\n"
        "──────────────────────\n\n"
        "📋 <b>Пришли токен своего бота:</b>"
    )


async def _handle_token_input(chat_id, text, user, session):
    if not TOKEN_RE.match(text):
        await motherbot.send_message(
            chat_id,
            "❌ Не похоже на токен бота.\n"
            "Формат: <code>1234567890:AABBcc...</code>\n"
            "Получи у @BotFather командой /token"
        )
        return

    await motherbot.send_message(chat_id, "⏳ Проверяю токен...")
    temp = TelegramClient(text)
    result = await temp.get_me()

    if not result.get("ok"):
        await motherbot.send_message(
            chat_id,
            f"❌ Токен недействителен.\n"
            f"<code>{result.get('description', 'Unknown')}</code>\n\n"
            "Проверь токен и попробуй снова."
        )
        return

    bot_info = result["result"]
    t_hash = token_to_hash(text)
    dup = (await session.execute(
        select(Bot).where(Bot.token_hash == t_hash))).scalar_one_or_none()
    if dup:
        await motherbot.send_message(
            chat_id, f"⚠️ Бот @{bot_info['username']} уже зарегистрирован. /mybots")
        return

    bot = Bot(
        user_telegram_id=user.telegram_id,
        token_encrypted=encrypt_token(text, settings.secret_key),
        token_hash=t_hash,
        bot_telegram_id=bot_info["id"],
        bot_name=bot_info["first_name"],
        bot_username=bot_info.get("username"),
        is_active=False,
    )
    session.add(bot)
    await session.flush()

    user.state = "waiting_feature_description"
    user.pending_bot_id = bot.id
    await session.flush()

    await motherbot.send_message(
        chat_id,
        f"✅ <b>Бот @{bot_info.get('username')} подключён!</b>\n\n"
        "Теперь <b>опиши что должен уметь твой бот</b> — "
        "я найду подходящих экспертов в библиотеке Extella.\n\n"
        "📝 <b>Примеры:</b>\n"
        "• <i>«переводить тексты на разные языки»</i>\n"
        "• <i>«отвечать на вопросы и помогать клиентам»</i>\n"
        "• <i>«генерировать посты для Instagram и Telegram»</i>\n"
        "• <i>«писать деловые письма и резюме»</i>\n"
        "• <i>«удалять фон с фотографий»</i>\n\n"
        "✍️ <b>Опиши желаемый функционал:</b>"
    )


async def _handle_feature_description(chat_id, text, user, session):
    bot_id = user.pending_bot_id
    if not bot_id:
        await motherbot.send_message(chat_id, "Что-то пошло не так. /start"); return
    bot = (await session.execute(
        select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
    if not bot:
        await motherbot.send_message(chat_id, "Бот не найден. /start"); return

    await motherbot.send_message(
        chat_id, f"🔍 Ищу экспертов по запросу «{text[:60]}»...")

    # Search Extella library
    matches = await extella.search_experts(text, limit=7)

    if not matches:
        await motherbot.send_message(
            chat_id,
            "😔 Не нашёл подходящих экспертов.\n\n"
            "Попробуй описать иначе, например:\n"
            "• <i>translate text to english</i>\n"
            "• <i>AI assistant answer questions</i>\n"
            "• <i>generate social media posts</i>\n"
            "• <i>send email draft</i>"
        )
        return

    # Save user description
    bot.system_prompt = text

    # Clear old experts, pre-create all found as active
    await session.execute(delete(BotExpert).where(BotExpert.bot_id == bot_id))
    for i, m in enumerate(matches):
        desc = m.get("description", m["name"])
        session.add(BotExpert(
            bot_id=bot_id,
            expert_name=m["name"],
            display_name=_clean_desc(desc),
            params_json={"__prompt_param__": _detect_prompt_param(m["name"], desc)},
            is_active=True,
            sort_order=i,
        ))
    await session.flush()

    user.state = "choosing_experts"
    await session.flush()

    selected = {m["name"] for m in matches}
    keyboard = _build_expert_keyboard(matches, selected, bot_id)

    await motherbot.send_message(
        chat_id,
        f"🎯 <b>Найдено {len(matches)} экспертов</b> под запрос «{text[:50]}»\n\n"
        "Все выбраны ✅ — нажми на эксперта чтобы <b>убрать</b> его.\n"
        "Когда готов — нажми <b>🚀 Активировать</b>",
        reply_markup=keyboard
    )


async def _cmd_mybots(chat_id, user, session):
    bots = (await session.execute(
        select(Bot).where(Bot.user_telegram_id == user.telegram_id)
    )).scalars().all()
    if not bots:
        await motherbot.send_message(chat_id, "У тебя пока нет ботов. /start"); return
    rows = [[{
        "text": f"{'✅' if b.is_active else '⏸'} @{b.bot_username or '?'} — {b.bot_name or '?'}",
        "callback_data": f"manage|{b.id}"
    }] for b in bots]
    rows.append([{"text": "➕ Добавить нового бота", "callback_data": "newbot"}])
    await motherbot.send_message(
        chat_id, f"🤖 Твои боты ({len(bots)}):", reply_markup={"inline_keyboard": rows})


# ── Callback handler ──────────────────────────────────────────────────────────

async def _handle_callback(callback: dict):
    cb_id = callback["id"]
    chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    data = callback.get("data", "")
    from_user = callback["from"]
    tg_id = from_user["id"]

    if data == "noop":
        await motherbot.answer_callback_query(cb_id, "Выберите хотя бы одну функцию!"); return
    if data == "newbot":
        async with get_session() as session:
            user = await _get_or_create_user(
                session, tg_id, from_user.get("username"), from_user.get("first_name"))
            await _cmd_start(chat_id, user, session)
        await motherbot.answer_callback_query(cb_id); return

    parts = data.split("|")
    action = parts[0] if parts else ""

    async with get_session() as session:
        user = await _get_or_create_user(
            session, tg_id, from_user.get("username"), from_user.get("first_name"))

        # Toggle expert on/off
        if action == "exp" and len(parts) == 3:
            expert_name, bot_id = parts[1], int(parts[2])
            bot = (await session.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tg_id:
                await motherbot.answer_callback_query(cb_id, "Бот не найден"); return
            exp = (await session.execute(
                select(BotExpert).where(
                    BotExpert.bot_id == bot_id,
                    BotExpert.expert_name == expert_name)
            )).scalar_one_or_none()
            if exp:
                exp.is_active = not exp.is_active
                await session.flush()
                label = "Добавлен ✅" if exp.is_active else "Убран ◻️"
                await motherbot.answer_callback_query(cb_id, label)
            else:
                await motherbot.answer_callback_query(cb_id, "Не найден"); return

            all_exps = (await session.execute(
                select(BotExpert).where(BotExpert.bot_id == bot_id)
                .order_by(BotExpert.sort_order)
            )).scalars().all()
            selected = {e.expert_name for e in all_exps if e.is_active}
            exp_dicts = [{"name": e.expert_name, "description": e.display_name or e.expert_name}
                         for e in all_exps]
            await motherbot.edit_message_text(
                chat_id, message_id,
                f"🎯 Выбрано: <b>{len(selected)} из {len(all_exps)}</b>\n\n"
                "Нажми на эксперта для переключения.\n"
                "Когда готов — <b>🚀 Активировать</b>",
                reply_markup=_build_expert_keyboard(exp_dicts, selected, bot_id)
            )

        # Research — ask user to re-describe
        elif action == "research" and len(parts) == 2:
            bot_id = int(parts[1])
            user.state = "waiting_feature_description"
            user.pending_bot_id = bot_id
            await session.flush()
            await motherbot.answer_callback_query(cb_id)
            await motherbot.send_message(chat_id, "✍️ Опиши заново что должен делать бот:")

        # Activate bot
        elif action == "activate" and len(parts) == 2:
            bot_id = int(parts[1])
            bot = (await session.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tg_id:
                await motherbot.answer_callback_query(cb_id, "Бот не найден"); return

            active_exps = (await session.execute(
                select(BotExpert).where(
                    BotExpert.bot_id == bot_id, BotExpert.is_active == True)
                .order_by(BotExpert.sort_order)
            )).scalars().all()

            if not active_exps:
                await motherbot.answer_callback_query(
                    cb_id, "Выберите хотя бы одну функцию!", show_alert=True); return

            await motherbot.answer_callback_query(cb_id, "⏳ Активирую...")

            raw_token = decrypt_token(bot.token_encrypted, settings.secret_key)
            webhook_url = f"{settings.railway_url}/bot/{bot.token_hash}/webhook"
            wh = await TelegramClient(raw_token).set_webhook(webhook_url)
            if not wh.get("ok"):
                await motherbot.send_message(
                    chat_id, f"❌ Webhook error: {wh.get('description', '?')}"); return

            bot.webhook_url = webhook_url
            bot.is_active = True
            user.state = "active"
            user.pending_bot_id = None
            await session.flush()

            # Build activation message
            expert_lines = []
            for e in active_exps:
                desc = e.display_name or e.expert_name
                expert_lines.append(f"• <b>{e.expert_name}</b>\n  {desc[:80]}")

            examples = _usage_examples(active_exps)
            routing_note = (
                "Система автоматически определяет какой эксперт подходит "
                "для каждого сообщения и вызывает его через Extella AI."
                if len(active_exps) > 1 else
                f"Все сообщения обрабатываются через <b>{active_exps[0].expert_name}</b>."
            )

            await motherbot.edit_message_text(
                chat_id, message_id,
                f"🎉 <b>Бот @{bot.bot_username} активирован!</b>\n\n"
                f"<b>Подключено экспертов: {len(active_exps)}</b>\n\n"
                + "\n\n".join(expert_lines) +
                "\n\n──────────────────────\n"
                f"<b>⚙️ Как это работает:</b>\n{routing_note}\n\n"
                f"<b>📝 Примеры запросов для пользователей:</b>\n{examples}\n\n"
                f"<b>🔧 Управление ботами:</b> /mybots\n"
                f"<b>🤖 Ваш бот:</b> @{bot.bot_username}"
            )

        # Manage bot info
        elif action == "manage" and len(parts) == 2:
            bot_id = int(parts[1])
            bot = (await session.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
            if not bot: await motherbot.answer_callback_query(cb_id, "Не найден"); return
            exps = (await session.execute(
                select(BotExpert).where(BotExpert.bot_id == bot_id, BotExpert.is_active == True)
            )).scalars().all()
            feat_list = "\n".join(f"• {e.display_name or e.expert_name}" for e in exps) or "—"
            desc = bot.system_prompt or "—"
            rows = [[{"text": "🗑 Деактивировать", "callback_data": f"deactivate|{bot_id}"}]]
            await motherbot.answer_callback_query(cb_id)
            await motherbot.send_message(
                chat_id,
                f"🤖 <b>@{bot.bot_username}</b>\n"
                f"Статус: {'✅ Активен' if bot.is_active else '⏸ Не активен'}\n"
                f"Запрос: <i>{desc[:80]}</i>\n\n"
                f"<b>Эксперты ({len(exps)}):</b>\n{feat_list}",
                reply_markup={"inline_keyboard": rows}
            )

        # Deactivate
        elif action == "deactivate" and len(parts) == 2:
            bot_id = int(parts[1])
            bot = (await session.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
            if bot and bot.user_telegram_id == tg_id:
                raw_token = decrypt_token(bot.token_encrypted, settings.secret_key)
                await TelegramClient(raw_token).delete_webhook()
                bot.is_active = False; await session.flush()
                await motherbot.answer_callback_query(cb_id, "Деактивирован")
                await motherbot.send_message(chat_id, f"⏸ @{bot.bot_username} деактивирован.")
            else:
                await motherbot.answer_callback_query(cb_id, "Не найден")
        else:
            await motherbot.answer_callback_query(cb_id)
