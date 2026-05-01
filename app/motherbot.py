import re
import logging
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from .database import User, Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import encrypt_token, token_to_hash, decrypt_token
from .config import settings

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r'^\d{8,12}:[A-Za-z0-9_-]{35,}$')
motherbot = TelegramClient(settings.motherbot_token)
extella = ExtellaClient(settings.extella_token)

# ── Improved classifier ──────────────────────────────────────────────────────
# Signs that an expert NEEDS local machine (filesystem access, local libs, etc.)
_LOCAL_SIGNALS = [
    # Explicit local flags
    "locally", "local file", "no api key needed", "no api key required",
    "no api", "works locally", "runs locally", "local machine", "local gpu",
    "local installation", "local environment", "local execution",
    "on your machine", "on device", "on your device",
    # Local libraries/tools
    "rembg", "selenium", "playwright", "puppeteer", "browser",
    "ffmpeg", "imagemagick", "ghostscript", "tesseract",
    "ollama", "llama.cpp", "whisper local",
    "pillow", "opencv", "cv2", "torch", "tensorflow",
    "sklearn", "scipy", "numpy local",
    "pyautogui", "pygetwindow", "pynput",
    "subprocess", "shell", "terminal",
    "sqlite", "local database",
    # Filesystem indicators
    "output_path", "output path", "saves to", "save to", "file path",
    "local path", "~/downloads", "~/documents", "~/desktop",
    "writes file", "reads file", "file system",
]

_CLOUD_SIGNALS = [
    # Explicit cloud/API flags
    "api key", "api_key", "cloud", "cloud api",
    "openai", "gpt", "chatgpt", "claude", "anthropic",
    "groq", "together", "cohere", "mistral",
    "replicate", "fal.ai", "fal ai",
    "remove.bg", "elevenlabs", "deepgram",
    "azure", "aws", "gcp", "google api",
    "http request", "rest api", "endpoint",
    "api call", "api token", "bearer token",
    "webhook", "cloud function",
]


def _classify(name: str, description: str) -> str:
    text = (name + " " + (description or "")).lower()
    # Count weighted signals
    ls = sum(3 for s in _LOCAL_SIGNALS if s in text)
    cs = sum(2 for s in _CLOUD_SIGNALS if s in text)
    # Extra weight: if description says "no api" = definitely local
    if "no api" in text or "locally" in text or "output_path" in text:
        ls += 5
    logger.debug(f"classify {name}: local={ls} cloud={cs}")
    return "local" if ls > cs else "cloud"

def _clean_desc(desc: str) -> str:
    for sep in ['. Parameters:', '\nParameters:', ' Parameters:']:
        if sep in desc:
            desc = desc.split(sep)[0]
            break
    return desc.strip()[:110]

def _detect_prompt_param(name: str, desc: str) -> str:
    n, d = name.lower(), desc.lower()
    if any(k in n for k in ('translat',)): return 'text'
    if any(k in n for k in ('image', 'photo', 'background')): return 'image_url'
    return 'prompt'

def _build_keyboard(all_exps: list[dict], selected: set[str], bot_id: int) -> dict:
    rows = []
    for exp in all_exps:
        name = exp['name']
        etype = exp.get('exec_type', _classify(name, exp.get('description', '')))
        badge = '☁️' if etype == 'cloud' else '💻'
        check = '✅' if name in selected else '◻️'
        label = _clean_desc(exp.get('description', name))
        if len(label) > 36: label = label[:36] + '...'
        rows.append([{'text': f'{check}{badge} {label}',
                       'callback_data': f'exp|{name}|{bot_id}'}])
    if selected:
        rows.append([{'text': '🚀 Активировать бота!',
                       'callback_data': f'activate|{bot_id}'}])
    else:
        rows.append([{'text': '☝️ Выберите хотя бы 1 функцию',
                       'callback_data': 'noop'}])
    rows.append([{'text': '🔄 Описать заново',
                  'callback_data': f'research|{bot_id}'}])
    return {'inline_keyboard': rows}

async def _get_or_create_user(session, tid, uname, fname):
    r = await session.execute(select(User).where(User.telegram_id == tid))
    u = r.scalar_one_or_none()
    if not u:
        u = User(telegram_id=tid, username=uname, first_name=fname, state='start')
        session.add(u)
        await session.flush()
    return u

async def handle_motherbot_update(data: dict):
    try:
        if msg := data.get('message'): await _handle_message(msg)
        elif cb := data.get('callback_query'): await _handle_callback(cb)
    except Exception as e:
        logger.error(f'motherbot: {e}', exc_info=True)

async def _handle_message(msg: dict):
    cid = msg['chat']['id']
    text = msg.get('text', '').strip()
    fu = msg['from']
    tid = fu['id']
    async with get_session() as s:
        u = await _get_or_create_user(s, tid, fu.get('username'), fu.get('first_name'))
        if text in ('/start', '/help'): await _cmd_start(cid, u, s)
        elif text == '/mybots': await _cmd_mybots(cid, u, s)
        elif text == '/connect': await _cmd_connect(cid, u, s)
        elif text == '/cancel':
            u.state = 'start'; u.pending_bot_id = None; await s.flush()
            await motherbot.send_message(cid, '/start — начать заново.')
        elif u.state == 'waiting_token': await _handle_token(cid, text, u, s)
        elif u.state == 'waiting_feature_description': await _handle_desc(cid, text, u, s)
        elif u.state == 'waiting_connect_token': await _handle_connect_token(cid, text, u, s)
        else: await motherbot.send_message(cid, 'Используй /start или /mybots')

async def _cmd_start(cid, u, s):
    u.state = 'waiting_token'; u.pending_bot_id = None; await s.flush()
    await motherbot.send_message(cid,
        '👋 <b>Extella Motherbot</b> — конструктор умных Telegram-ботов.\n\n'
        'Я подберу AI-функции из библиотеки <b>Extella</b> под твой запрос.\n'
        '☁️ = работает на сервере сразу\n'
        '💻 = нужен локальный компьютер (/connect)\n\n'
        '──────────────────────\n'
        '<b>Шаг 1</b> — создай бота у @BotFather (/newbot)\n'
        '<b>Шаг 2</b> — пришли мне токен\n'
        '<b>Шаг 3</b> — опиши что должен делать бот\n'
        '──────────────────────\n\n'
        '📋 <b>Пришли токен:</b>')

async def _handle_token(cid, text, u, s):
    if not TOKEN_RE.match(text):
        await motherbot.send_message(cid, '❌ Не похоже на токен.\n'
            'Формат: <code>1234567890:AABBcc...</code>\n'
            'Получи у @BotFather командой /token')
        return
    await motherbot.send_message(cid, '⏳ Проверяю...')
    gm = await TelegramClient(text).get_me()
    if not gm.get('ok'):
        await motherbot.send_message(cid, f'❌ Токен недействителен.\n<code>{gm.get("description", "?")}</code>')
        return
    bi = gm['result']; th = token_to_hash(text)
    dup = (await s.execute(select(Bot).where(Bot.token_hash == th))).scalar_one_or_none()
    if dup:
        await motherbot.send_message(cid, f'⚠️ @{bi["username"]} уже есть. /mybots')
        return
    bot = Bot(user_telegram_id=u.telegram_id,
              token_encrypted=encrypt_token(text, settings.secret_key),
              token_hash=th, bot_telegram_id=bi['id'],
              bot_name=bi['first_name'], bot_username=bi.get('username'), is_active=False)
    s.add(bot); await s.flush()
    u.state = 'waiting_feature_description'; u.pending_bot_id = bot.id; await s.flush()
    await motherbot.send_message(cid,
        f'✅ <b>Бот @{bi.get("username")} подключён!</b>\n\n'
        'Опиши что должен делать твой бот — я подберу нужных экспертов.\n\n'
        '📝 <b>Примеры:</b>\n'
        '• <i>переводить тексты на разные языки</i>\n'
        '• <i>отвечать на вопросы клиентов</i>\n'
        '• <i>генерировать посты для соцсетей</i>\n'
        '• <i>удалять фон с фотографий</i>\n\n'
        '✍️ <b>Опиши желаемый функционал:</b>')

async def _handle_desc(cid, text, u, s):
    bot_id = u.pending_bot_id
    if not bot_id: await motherbot.send_message(cid, '/start'); return
    bot = (await s.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
    if not bot: await motherbot.send_message(cid, '/start'); return
    await motherbot.send_message(cid, f'🔍 Ищу экспертов...')
    matches = await extella.search_experts(text, limit=7)
    if not matches:
        await motherbot.send_message(cid, '☹️ Не нашёл. Опиши иначе.'); return
    bot.system_prompt = text
    await s.execute(delete(BotExpert).where(BotExpert.bot_id == bot_id))
    for i, m in enumerate(matches):
        desc = m.get('description', m['name'])
        etype = _classify(m['name'], desc)
        s.add(BotExpert(bot_id=bot_id, expert_name=m['name'],
                        display_name=_clean_desc(desc), exec_type=etype,
                        params_json={'__prompt_param__': _detect_prompt_param(m['name'], desc)},
                        is_active=True, sort_order=i))
    await s.flush()
    u.state = 'choosing_experts'; await s.flush()
    selected = {m['name'] for m in matches}
    exps_with_type = [{'name': m['name'], 'description': m.get('description',''),
                        'exec_type': _classify(m['name'], m.get('description',''))}
                       for m in matches]
    local_n = sum(1 for x in exps_with_type if x['exec_type'] == 'local')
    legend = ''
    if local_n:
        legend = (f'\n\n☁️ = на сервере  💻 = на твоём компьютере '
                   f'({local_n} шт.)')
    await motherbot.send_message(cid,
        f'🎯 <b>Найдено {len(matches)} экспертов</b>{legend}\n\n'
        'Все выбраны ✅ — нажми чтобы убрать.\n'
        'Когда готов — <b>🚀 Активировать</b>',
        reply_markup=_build_keyboard(exps_with_type, selected, bot_id))

async def _cmd_mybots(cid, u, s):
    bots = (await s.execute(select(Bot).where(Bot.user_telegram_id == u.telegram_id))).scalars().all()
    if not bots: await motherbot.send_message(cid, '/start'); return
    rows = [[{'text': f'{'\u2705' if b.is_active else '\u23f8\ufe0f'} @{b.bot_username or '?'} \u2014 {b.bot_name or '?'}',
              'callback_data': f'manage|{b.id}'}] for b in bots]
    rows.append([{'text': '\u2795 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0431\u043e\u0442\u0430', 'callback_data': 'newbot'}])
    await motherbot.send_message(cid, f'🤖 Боты ({len(bots)}):', reply_markup={'inline_keyboard': rows})

async def _cmd_connect(cid, u, s):
    bots = (await s.execute(
        select(Bot).where(Bot.user_telegram_id == u.telegram_id, Bot.is_active == True)
    )).scalars().all()
    if not bots:
        await motherbot.send_message(cid, 'Нет активных ботов. /start'); return
    u.state = 'waiting_connect_token'
    u.pending_bot_id = bots[-1].id
    await s.flush()
    await motherbot.send_message(cid,
        '🔗 <b>Подключение локальной машины</b>\n\n'
        '💻-эксперты (rembg, ffmpeg, ollama и т.д.) работают на твоём компьютере.\n\n'
        '<b>Шаг 1.</b> Скачай Extella Desktop: extella.ai/download\n'
        '<b>Шаг 2.</b> Открой Extella → Settings → API Tokens\n'
        '<b>Шаг 3.</b> Нажми Generate Token, скопируй и вставь сюда:')

async def _handle_connect_token(cid, text, u, s):
    await motherbot.send_message(cid, '⏳ Проверяю токен...')
    tmp = ExtellaClient(text)
    valid = await tmp.validate_token(text)
    if not valid:
        await motherbot.send_message(cid, '❌ Токен недействителен. Сгенерируй новый.')
        return
    targets = await tmp.list_targets(text)
    if not targets:
        await motherbot.send_message(cid,
            '⚠️ Устройства не найдены.\n'
            'Убедись что Extella Desktop открыт и запущен на твоём компьютере.')
        return
    first = targets[0]
    target_id = first.get('target') or first.get('id', '')
    target_name = first.get('description', 'My Machine')[:50]
    bot_id = u.pending_bot_id
    bot = (await s.execute(select(Bot).where(Bot.id == bot_id))).scalar_one_or_none()
    if bot:
        bot.user_extella_token_enc = encrypt_token(text, settings.secret_key)
        bot.user_target_id = target_id
    u.state = 'active'; u.pending_bot_id = None; await s.flush()
    await motherbot.send_message(cid,
        f'✅ <b>Машина подключена!</b>\n\n'
        f'Устройство: <b>{target_name}</b>\n'
        '💻-эксперты теперь будут запускаться на твоём компьютере.')

async def _handle_callback(cb: dict):
    cbid = cb['id']; cid = cb['message']['chat']['id']
    mid = cb['message']['message_id']; data = cb.get('data', '')
    fu = cb['from']; tid = fu['id']
    if data == 'noop':
        await motherbot.answer_callback_query(cbid, 'Выберите хотя бы 1 функцию!'); return
    if data == 'newbot':
        async with get_session() as s:
            u = await _get_or_create_user(s, tid, fu.get('username'), fu.get('first_name'))
            await _cmd_start(cid, u, s)
        await motherbot.answer_callback_query(cbid); return
    parts = data.split('|'); action = parts[0] if parts else ''
    async with get_session() as s:
        u = await _get_or_create_user(s, tid, fu.get('username'), fu.get('first_name'))
        if action == 'exp' and len(parts) == 3:
            ename, bid = parts[1], int(parts[2])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, 'Не найден'); return
            ex = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.expert_name == ename))).scalar_one_or_none()
            if ex:
                ex.is_active = not ex.is_active; await s.flush()
                await motherbot.answer_callback_query(cbid, '✅ Добавлено' if ex.is_active else '◻️ Убрано')
            all_e = (await s.execute(select(BotExpert).where(BotExpert.bot_id == bid)
                .order_by(BotExpert.sort_order))).scalars().all()
            sel = {e.expert_name for e in all_e if e.is_active}
            ed = [{'name': e.expert_name, 'description': e.display_name or '',
                   'exec_type': e.exec_type} for e in all_e]
            await motherbot.edit_message_text(cid, mid,
                f'Выбрано: <b>{len(sel)}</b>\nНажми для переключения.',
                reply_markup=_build_keyboard(ed, sel, bid))
        elif action == 'research' and len(parts) == 2:
            bid = int(parts[1]); u.state = 'waiting_feature_description'
            u.pending_bot_id = bid; await s.flush()
            await motherbot.answer_callback_query(cbid)
            await motherbot.send_message(cid, '✍️ Опиши заново:')
        elif action == 'activate' and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, 'Не найден'); return
            exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
            if not exps:
                await motherbot.answer_callback_query(cbid, 'Выберите хотя бы 1!', show_alert=True); return
            await motherbot.answer_callback_query(cbid, '⏳ Активирую...')
            raw = decrypt_token(bot.token_encrypted, settings.secret_key)
            wh = await TelegramClient(raw).set_webhook(
                f'{settings.railway_url}/bot/{bot.token_hash}/webhook')
            if not wh.get('ok'):
                await motherbot.send_message(cid, f'❌ Webhook: {wh.get("description","?")}'); return
            bot.webhook_url = f'{settings.railway_url}/bot/{bot.token_hash}/webhook'
            bot.is_active = True; u.state = 'active'; u.pending_bot_id = None
            await s.flush()
            local_exps = [e for e in exps if e.exec_type == 'local']
            cloud_exps = [e for e in exps if e.exec_type == 'cloud']
            lines = []
            for e in exps:
                tag = '☁️' if e.exec_type == 'cloud' else '💻'
                lines.append(f'{tag} <b>{e.expert_name}</b>\n   {(e.display_name or "")[:70]}')
            connect_note = ''
            if local_exps and not bot.user_target_id:
                connect_note = (f'\n\n⚠️ <b>{len(local_exps)} эксп. '
                                'требуют локальный компьютер (💻)</b>\n'
                                'Чтобы их включить — отправь /connect')
            await motherbot.edit_message_text(cid, mid,
                f'🎉 <b>@{bot.bot_username} активирован!</b>\n\n'
                f'<b>Экспертов: {len(exps)}</b>\n\n'
                + '\n\n'.join(lines) + connect_note +
                f'\n\n🤖 @{bot.bot_username} уже работает!')
        elif action == 'manage' and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot: await motherbot.answer_callback_query(cbid, 'Нет'); return
            exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
            fl = '\n'.join(f'{'\u2601\ufe0f' if e.exec_type=="cloud" else "\U0001f4bb"} {e.display_name or e.expert_name}' for e in exps) or '—'
            rows = [[{'text': '🗑 Деактивировать', 'callback_data': f'deactivate|{bid}'}]]
            if not bot.user_target_id:
                rows.insert(0, [{'text': '🔗 Подключить машину', 'callback_data': f'connect_bot|{bid}'}])
            await motherbot.answer_callback_query(cbid)
            await motherbot.send_message(cid,
                f'🤖 @{bot.bot_username} | {'\u2705' if bot.is_active else '\u23f8\ufe0f'}\n\n'
                f'<b>Эксперты:</b>\n{fl}\n\n'
                f'💻 Машина: {'\u2705 подключена' if bot.user_target_id else '\u274c нет (/connect)'}',
                reply_markup={'inline_keyboard': rows})
        elif action == 'connect_bot' and len(parts) == 2:
            bid = int(parts[1]); u.state = 'waiting_connect_token'
            u.pending_bot_id = bid; await s.flush()
            await motherbot.answer_callback_query(cbid)
            await _cmd_connect(cid, u, s)
        elif action == 'deactivate' and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if bot and bot.user_telegram_id == tid:
                raw = decrypt_token(bot.token_encrypted, settings.secret_key)
                await TelegramClient(raw).delete_webhook()
                bot.is_active = False; await s.flush()
                await motherbot.answer_callback_query(cbid, 'Деактивирован')
                await motherbot.send_message(cid, f'⏸️ @{bot.bot_username} остановлен.')
            else: await motherbot.answer_callback_query(cbid, 'Нет')
        else: await motherbot.answer_callback_query(cbid)
