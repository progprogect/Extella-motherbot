import re, logging, json
from sqlalchemy import select, delete
from .database import User, Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import encrypt_token, token_to_hash, decrypt_token
from .config import settings
from .key_manager import SERVICES, get_required_keys, set_bot_key, has_required_keys

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"^\d{8,12}:[A-Za-z0-9_-]{35,}$")
motherbot = TelegramClient(settings.motherbot_token)
extella = ExtellaClient(settings.extella_token)


# ── Classifier ────────────────────────────────────────────────────────────────
_KNOWN_LOCAL = {
    "image_enhance","improve_photo_quality","remove_background_local","remove_bg_local",
    "video_enhance","video_upscale","text_to_speech","voice_clone_tortoise",
    "transcribe_audio_file","pdf_edit","edit_pdf","merge_pdf","split_pdf",
    "organize_files","scan_folder","convert_file","file_converter",
}

def _is_local(name: str, desc: str = "") -> bool:
    if name in _KNOWN_LOCAL: return True
    t = (name + " " + desc).lower()
    return any(w in t for w in [
        "pillow","opencv","ffmpeg","rembg","ollama","output_path",
        "saves to","local file","no api key needed","locally","subprocess",
    ])


def _clean_desc(desc: str) -> str:
    for sep in [". Parameters:", "\nParameters:", " Parameters:"]:
        if sep in desc: desc = desc.split(sep)[0]; break
    return desc.strip()[:110]


def _detect_prompt_param(name: str, desc: str) -> str:
    n, d = name.lower(), desc.lower()
    if "translat" in n: return "text"
    if any(k in n for k in ("image","photo","background")): return "image_url"
    return "prompt"


def _build_expert_kb(all_exps: list[dict], selected: set[str], bot_id: int) -> dict:
    rows = []
    for exp in all_exps:
        name = exp["name"]
        local = _is_local(name, exp.get("description",""))
        has_runner = name in CLOUD_RUNNERS
        badge = "☁️" if (has_runner or not local) else "💻"
        check = "✅" if name in selected else "◻️"
        label = _clean_desc(exp.get("description", name))
        if len(label) > 35: label = label[:35] + "..."
        rows.append([{"text": f"{check}{badge} {label}",
                       "callback_data": f"exp|{name}|{bot_id}"}])
    if selected:
        rows.append([{"text": "🚀 Продолжить", "callback_data": f"activate|{bot_id}"}])
    else:
        rows.append([{"text": "☝️ Выберите хотя бы 1 функцию", "callback_data": "noop"}])
    rows.append([{"text": "🔄 Описать заново", "callback_data": f"research|{bot_id}"}])
    return {"inline_keyboard": rows}


async def _get_or_create_user(session, tid, uname, fname):
    r = await session.execute(select(User).where(User.telegram_id == tid))
    u = r.scalar_one_or_none()
    if not u:
        u = User(telegram_id=tid, username=uname, first_name=fname, state="start")
        session.add(u); await session.flush()
    return u


async def handle_motherbot_update(data: dict):
    try:
        if msg := data.get("message"): await _handle_message(msg)
        elif cb := data.get("callback_query"): await _handle_callback(cb)
    except Exception as e:
        logger.error(f"motherbot: {e}", exc_info=True)


async def _handle_message(msg: dict):
    cid = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    fu = msg["from"]; tid = fu["id"]
    async with get_session() as s:
        u = await _get_or_create_user(s, tid, fu.get("username"), fu.get("first_name"))
        if text in ("/start", "/help"): await _cmd_start(cid, u, s)
        elif text == "/mybots": await _cmd_mybots(cid, u, s)
        elif text == "/apikeys": await _cmd_apikeys(cid, u, s)
        elif text == "/connect": await _cmd_connect(cid, u, s)
        elif text == "/cancel":
            u.state = "start"; u.pending_bot_id = None; u.pending_key_name = None
            await s.flush()
            await motherbot.send_message(cid, "Отменено. /start — начать заново.")
        elif u.state == "waiting_token": await _handle_token(cid, text, u, s)
        elif u.state == "waiting_feature_description": await _handle_desc(cid, text, u, s)
        elif u.state == "waiting_api_key": await _handle_api_key(cid, text, u, s)
        elif u.state == "waiting_connect_token": await _handle_connect_token(cid, text, u, s)
        else: await motherbot.send_message(cid, "Используй /start или /mybots")


async def _cmd_start(cid, u, s):
    u.state = "waiting_token"; u.pending_bot_id = None; u.pending_key_name = None
    await s.flush()
    await motherbot.send_message(cid,
        "👋 <b>Extella Motherbot</b> — конструктор умных Telegram-ботов.\n\n"
        "Подберу AI-функции из библиотеки Extella под твой запрос.\n\n"
        "☁️ = работает в облаке  💻 = нужен локальный ПК\n\n"
        "──────────────────────\n"
        "<b>Шаг 1</b> — создай бота у @BotFather (/newbot)\n"
        "<b>Шаг 2</b> — пришли токен\n"
        "<b>Шаг 3</b> — опиши функционал\n"
        "<b>Шаг 4</b> — добавь API-ключи (если нужны)\n"
        "──────────────────────\n\n"
        "📋 <b>Пришли токен своего бота:</b>")


async def _handle_token(cid, text, u, s):
    if not TOKEN_RE.match(text):
        await motherbot.send_message(cid,
            "❌ Не похоже на токен.\nФормат: <code>1234567890:AABBcc...</code>")
        return
    await motherbot.send_message(cid, "⏳ Проверяю...")
    gm = await TelegramClient(text).get_me()
    if not gm.get("ok"):
        await motherbot.send_message(cid, f"❌ Токен недействителен.\n<code>{gm.get('description','?')}</code>")
        return
    bi = gm["result"]; th = token_to_hash(text)
    dup = (await s.execute(select(Bot).where(Bot.token_hash == th))).scalar_one_or_none()
    if dup:
        await motherbot.send_message(cid, f"⚠️ @{bi['username']} уже зарегистрирован. /mybots")
        return
    bot = Bot(user_telegram_id=u.telegram_id,
              token_encrypted=encrypt_token(text, settings.secret_key),
              token_hash=th, bot_telegram_id=bi["id"],
              bot_name=bi["first_name"], bot_username=bi.get("username"), is_active=False)
    s.add(bot); await s.flush()
    u.state = "waiting_feature_description"; u.pending_bot_id = bot.id; await s.flush()
    await motherbot.send_message(cid,
        f"✅ <b>@{bi.get('username')} подключён!</b>\n\n"
        "Опиши что должен делать бот:\n\n"
        "📝 Примеры:\n"
        "• <i>«переводить тексты на разные языки»</i>\n"
        "• <i>«отвечать на вопросы клиентов»</i>\n"
        "• <i>«генерировать посты для соцсетей»</i>\n"
        "• <i>«удалять фон с фотографий»</i>\n\n"
        "✍️ <b>Желаемый функционал:</b>")


async def _handle_desc(cid, text, u, s):
    bid = u.pending_bot_id
    if not bid: await motherbot.send_message(cid, "/start"); return
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if not bot: await motherbot.send_message(cid, "/start"); return
    await motherbot.send_message(cid, f"🔍 Ищу экспертов...")
    matches = await extella.search_experts(text, limit=7)
    if not matches:
        await motherbot.send_message(cid, "😔 Не нашёл. Опиши иначе."); return
    bot.system_prompt = text
    await s.execute(delete(BotExpert).where(BotExpert.bot_id == bid))
    for i, m in enumerate(matches):
        desc = m.get("description", m["name"])
        s.add(BotExpert(bot_id=bid, expert_name=m["name"],
                        display_name=_clean_desc(desc),
                        exec_type="local" if _is_local(m["name"], desc) else "cloud",
                        params_json={"__prompt_param__": _detect_prompt_param(m["name"], desc)},
                        is_active=True, sort_order=i))
    await s.flush()
    u.state = "choosing_experts"; await s.flush()
    selected = {m["name"] for m in matches}
    exps_dicts = [{"name": m["name"], "description": m.get("description","")} for m in matches]
    await motherbot.send_message(cid,
        f"🎯 <b>Найдено {len(matches)} экспертов</b>\n\n"
        "☁️ = облако (работает сразу)\n"
        "💻 = локальный ПК (нужна Extella Desktop)\n\n"
        "Все выбраны ✅ — нажми чтобы убрать.\nКогда готов → <b>🚀 Продолжить</b>",
        reply_markup=_build_expert_kb(exps_dicts, selected, bid))


async def _handle_api_key(cid, text, u, s):
    """Handle incoming API key from user."""
    bid = u.pending_bot_id; key_name = u.pending_key_name
    if not bid or not key_name:
        await motherbot.send_message(cid, "/start"); return
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if not bot: await motherbot.send_message(cid, "/start"); return

    if text.lower() in ("skip", "пропустить", "/skip"):
        # Skip this key — continue to next or activate
        await motherbot.send_message(cid, f"⏭ Ключ {key_name} пропущен.")
    else:
        # Validate basic format
        svc = SERVICES.get(key_name, {})
        example = svc.get("example", "")
        prefix = example.split("-")[0].split(":")[0] if example else ""
        set_bot_key(bot, key_name, text.strip(), settings.secret_key)
        await s.flush()
        await motherbot.send_message(cid, f"✅ Ключ <b>{svc.get('display', key_name)}</b> сохранён!")

    # Check if more keys needed
    exps = (await s.execute(
        select(BotExpert).where(BotExpert.bot_id == bid, BotExpert.is_active == True)
    )).scalars().all()
    expert_names = [e.expert_name for e in exps]
    all_ok, missing = has_required_keys(bot, expert_names, settings.secret_key)

    # Filter out already-asked key
    remaining = [k for k in missing if k != key_name]
    if remaining:
        await _ask_next_key(cid, u, s, bot, remaining[0])
        return

    # All keys collected (or skipped) — activate
    u.state = "activating"; u.pending_key_name = None; await s.flush()
    await _do_activate(cid, u, s, bot, exps)


async def _ask_next_key(cid, u, s, bot, key_name: str):
    """Ask user for a specific API key."""
    svc = SERVICES[key_name]
    u.state = "waiting_api_key"; u.pending_key_name = key_name; await s.flush()
    skip_kb = {"inline_keyboard": [[
        {"text": f"⏭ {svc['skip_text']}", "callback_data": f"skip_key|{key_name}"}
    ]]}
    await motherbot.send_message(cid,
        f"🔑 <b>Требуется ключ: {svc['display']}</b>\n\n"
        + svc["instructions"]
        + f"\n\n<code>Пример: {svc['example']}</code>",
        reply_markup=skip_kb)


async def _cmd_mybots(cid, u, s):
    bots = (await s.execute(select(Bot).where(Bot.user_telegram_id == u.telegram_id))).scalars().all()
    if not bots: await motherbot.send_message(cid, "/start"); return
    rows = [[{"text": f"{'✅' if b.is_active else '⏸'} @{b.bot_username or '?'} — {b.bot_name or '?'}",
              "callback_data": f"manage|{b.id}"}] for b in bots]
    rows.append([{"text": "➕ Добавить бота", "callback_data": "newbot"}])
    await motherbot.send_message(cid, f"🤖 Боты ({len(bots)}):", reply_markup={"inline_keyboard": rows})


async def _cmd_apikeys(cid, u, s):
    """Show/manage API keys for active bots."""
    bots = (await s.execute(
        select(Bot).where(Bot.user_telegram_id == u.telegram_id, Bot.is_active == True)
    )).scalars().all()
    if not bots:
        await motherbot.send_message(cid, "У тебя нет активных ботов. /start"); return
    bot = bots[-1]  # Latest active bot
    u.state = "waiting_api_key"; u.pending_bot_id = bot.id; u.pending_key_name = "fal_api_key"
    await s.flush()
    svc = SERVICES["fal_api_key"]
    await motherbot.send_message(cid,
        f"🔑 <b>API ключи для @{bot.bot_username}</b>\n\n"
        + svc["instructions"],
        reply_markup={"inline_keyboard": [[
            {"text": "⏭ Отмена", "callback_data": "cancel_apikey"}
        ]]})


async def _cmd_connect(cid, u, s):
    bots = (await s.execute(
        select(Bot).where(Bot.user_telegram_id == u.telegram_id, Bot.is_active == True)
    )).scalars().all()
    if not bots: await motherbot.send_message(cid, "Нет активных ботов. /start"); return
    u.state = "waiting_connect_token"; u.pending_bot_id = bots[-1].id; await s.flush()
    await motherbot.send_message(cid,
        "🔗 <b>Подключение локальной машины</b>\n\n"
        "Для 💻-функций нужен Extella Desktop на вашем ПК.\n\n"
        "<b>Шаг 1.</b> Скачайте Extella Desktop: extella.ai/download\n"
        "<b>Шаг 2.</b> Откройте Extella → Settings → API Tokens\n"
        "<b>Шаг 3.</b> Создайте токен и пришлите сюда:")


async def _handle_connect_token(cid, text, u, s):
    await motherbot.send_message(cid, "⏳ Проверяю токен...")
    tmp = ExtellaClient(text)
    if not await tmp.validate_token(text):
        await motherbot.send_message(cid, "❌ Токен недействителен."); return
    targets = await tmp.list_targets(text)
    if not targets:
        await motherbot.send_message(cid,
            "⚠️ Устройства не найдены.\nУбедись что Extella Desktop открыт и запущен."); return
    first = targets[0]
    target_id = first.get("target") or first.get("id", "")
    target_name = first.get("description", "My Machine")[:50]
    bid = u.pending_bot_id
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if bot:
        bot.user_extella_token_enc = encrypt_token(text, settings.secret_key)
        bot.user_target_id = target_id
    u.state = "active"; u.pending_bot_id = None; await s.flush()
    await motherbot.send_message(cid,
        f"✅ <b>Машина подключена!</b>\n\nУстройство: <b>{target_name}</b>\n"
        "💻-функции теперь будут запускаться на вашем компьютере.")


async def _handle_callback(cb: dict):
    cbid = cb["id"]; cid = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]; data = cb.get("data","")
    fu = cb["from"]; tid = fu["id"]
    if data == "noop":
        await motherbot.answer_callback_query(cbid, "Выберите хотя бы 1 функцию!"); return
    if data == "cancel_apikey":
        await motherbot.answer_callback_query(cbid)
        async with get_session() as s:
            u = await _get_or_create_user(s, tid, fu.get("username"), fu.get("first_name"))
            u.state = "active"; u.pending_key_name = None; await s.flush()
        await motherbot.send_message(cid, "✅ Ключ не добавлен. /apikeys — добавить позже.")
        return
    if data == "newbot":
        async with get_session() as s:
            u = await _get_or_create_user(s, tid, fu.get("username"), fu.get("first_name"))
            await _cmd_start(cid, u, s)
        await motherbot.answer_callback_query(cbid); return

    parts = data.split("|"); action = parts[0] if parts else ""
    async with get_session() as s:
        u = await _get_or_create_user(s, tid, fu.get("username"), fu.get("first_name"))

        if action == "skip_key" and len(parts) == 2:
            key_name = parts[1]
            await motherbot.answer_callback_query(cbid, "Пропущено")
            bot = (await s.execute(select(Bot).where(Bot.id == u.pending_bot_id))).scalar_one_or_none()
            exps = (await s.execute(
                select(BotExpert).where(BotExpert.bot_id == u.pending_bot_id, BotExpert.is_active == True)
            )).scalars().all() if bot else []
            expert_names = [e.expert_name for e in exps]
            _, missing = has_required_keys(bot, expert_names, settings.secret_key) if bot else (True, [])
            remaining = [k for k in missing if k != key_name]
            if remaining:
                await _ask_next_key(cid, u, s, bot, remaining[0])
            else:
                u.state = "activating"; u.pending_key_name = None; await s.flush()
                await _do_activate(cid, u, s, bot, exps)

        elif action == "exp" and len(parts) == 3:
            ename, bid = parts[1], int(parts[2])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, "Не найден"); return
            ex = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.expert_name == ename))).scalar_one_or_none()
            if ex:
                ex.is_active = not ex.is_active; await s.flush()
                await motherbot.answer_callback_query(cbid,
                    "✅ Добавлен" if ex.is_active else "◻️ Убран")
            all_e = (await s.execute(select(BotExpert).where(BotExpert.bot_id == bid)
                .order_by(BotExpert.sort_order))).scalars().all()
            sel = {e.expert_name for e in all_e if e.is_active}
            ed = [{"name": e.expert_name, "description": e.display_name or ""} for e in all_e]
            await motherbot.edit_message_text(cid, mid,
                f"Выбрано: <b>{len(sel)}</b> из {len(all_e)}\nНажми для переключения.",
                reply_markup=_build_expert_kb(ed, sel, bid))

        elif action == "research" and len(parts) == 2:
            bid = int(parts[1]); u.state = "waiting_feature_description"
            u.pending_bot_id = bid; await s.flush()
            await motherbot.answer_callback_query(cbid)
            await motherbot.send_message(cid, "✍️ Опиши заново:")

        elif action == "activate" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, "Не найден"); return
            exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
            if not exps:
                await motherbot.answer_callback_query(cbid, "Выберите хотя бы 1!", show_alert=True); return
            await motherbot.answer_callback_query(cbid, "⏳ Проверяю...")

            # Check if any selected experts need user-provided keys
            expert_names = [e.expert_name for e in exps]
            all_ok, missing = has_required_keys(bot, expert_names, settings.secret_key)

            if missing:
                # Ask for first missing key
                u.pending_bot_id = bid; u.state = "choosing_experts"; await s.flush()
                await motherbot.send_message(cid,
                    f"⚙️ <b>Выбрано {len(exps)} экспертов</b>\n\n"
                    "Некоторые функции требуют API-ключи сторонних сервисов.\n"
                    "Ключи хранятся зашифровано и используются только вашим ботом.")
                await _ask_next_key(cid, u, s, bot, missing[0])
            else:
                await _do_activate(cid, u, s, bot, exps, message_id=mid)

        elif action == "manage" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot: await motherbot.answer_callback_query(cbid, "Нет"); return
            exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
            fl = "\n".join(
                f"{'☁️' if e.expert_name in CLOUD_RUNNERS else '💻'} {e.display_name or e.expert_name}"
                for e in exps) or "—"
            rows = [
                [{"text": "🔑 Управление ключами", "callback_data": f"manage_keys|{bid}"}],
                [{"text": "🗑 Деактивировать", "callback_data": f"deactivate|{bid}"}],
            ]
            if not bot.user_target_id:
                rows.insert(0, [{"text": "🔗 Подключить ПК", "callback_data": f"connect_bot|{bid}"}])
            await motherbot.answer_callback_query(cbid)
            await motherbot.send_message(cid,
                f"🤖 @{bot.bot_username} | {'✅' if bot.is_active else '⏸'}\n\n"
                f"<b>Функции:</b>\n{fl}\n\n"
                f"💻 ПК: {'✅ подключён' if bot.user_target_id else '❌ нет (/connect)'}",
                reply_markup={"inline_keyboard": rows})

        elif action == "manage_keys" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot: return
            u.state = "waiting_api_key"; u.pending_bot_id = bid
            u.pending_key_name = "fal_api_key"; await s.flush()
            await motherbot.answer_callback_query(cbid)
            svc = SERVICES["fal_api_key"]
            await motherbot.send_message(cid,
                f"🔑 <b>Ключ {svc['display']}</b>\n\n" + svc["instructions"],
                reply_markup={"inline_keyboard": [[
                    {"text": "⏭ Отмена", "callback_data": "cancel_apikey"}]]})

        elif action == "connect_bot" and len(parts) == 2:
            bid = int(parts[1]); u.state = "waiting_connect_token"
            u.pending_bot_id = bid; await s.flush()
            await motherbot.answer_callback_query(cbid)
            await _cmd_connect(cid, u, s)

        elif action == "deactivate" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if bot and bot.user_telegram_id == tid:
                raw = decrypt_token(bot.token_encrypted, settings.secret_key)
                await TelegramClient(raw).delete_webhook()
                bot.is_active = False; await s.flush()
                await motherbot.answer_callback_query(cbid, "Деактивирован")
                await motherbot.send_message(cid, f"⏸ @{bot.bot_username} остановлен.")
            else: await motherbot.answer_callback_query(cbid, "Нет")
        else:
            await motherbot.answer_callback_query(cbid)


async def _do_activate(cid, u, s, bot, exps, message_id: int = None):
    """Final step: register webhook and activate bot."""
    raw = decrypt_token(bot.token_encrypted, settings.secret_key)
    webhook_url = f"{settings.railway_url}/bot/{bot.token_hash}/webhook"
    wh = await TelegramClient(raw).set_webhook(webhook_url)
    if not wh.get("ok"):
        await motherbot.send_message(cid, f"❌ Webhook: {wh.get('description','?')}"); return
    bot.webhook_url = webhook_url; bot.is_active = True
    u.state = "active"; u.pending_bot_id = None; u.pending_key_name = None
    await s.flush()

    local_exps = [e for e in exps if _is_local(e.expert_name, e.display_name or "")]
    lines = []
    for e in exps:
        tag = "☁️" if e.expert_name in CLOUD_RUNNERS else "💻"
        lines.append(f"{tag} <b>{e.expert_name}</b>\n   {(e.display_name or '')[:60]}")

    conn_note = ""
    if local_exps and not bot.user_target_id:
        conn_note = (f"\n\n⚠️ {len(local_exps)} функц. требуют ПК.\n"
                     "Отправь /connect чтобы подключить компьютер.")

    text = (f"🎉 <b>@{bot.bot_username} активирован!</b>\n\n"
            f"<b>Экспертов: {len(exps)}</b>\n\n"
            + "\n\n".join(lines) + conn_note +
            f"\n\n🤖 @{bot.bot_username} уже работает!")

    if message_id:
        await motherbot.edit_message_text(cid, message_id, text)
    else:
        await motherbot.send_message(cid, text)