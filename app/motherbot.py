import re
import logging
from sqlalchemy import select, delete

from .database import User, Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import encrypt_token, token_to_hash, decrypt_token
from .config import settings
from .key_manager import get_bot_keys, set_bot_key

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"^\d{8,12}:[A-Za-z0-9_-]{35,}$")
motherbot = TelegramClient(settings.motherbot_token)
extella = ExtellaClient(settings.extella_token)

_KNOWN_LOCAL = {
    "image_enhance","improve_photo_quality","remove_background_local","remove_bg_local",
    "video_enhance","video_upscale","text_to_speech","voice_clone_tortoise",
    "transcribe_audio_file","audio_to_text_free","pdf_edit","edit_pdf",
    "merge_pdf","split_pdf","organize_files","file_organizer","scan_folder",
    "convert_file","file_converter","save_presentation_pptx","build_presentation",
    "get_clipboard","read_local_file",
}
_LOCAL_SIGNALS = [
    "pillow","opencv","ffmpeg","rembg","ollama","output_path",
    "saves to","local file","no api key needed","no api key required",
    "locally","local machine","subprocess","filesystem",
]
_EXTELLA_ABOUT = (
    "\U0001f916 <b>What is Extella?</b>\n"
    "Extella is an AI execution platform that runs AI experts on any machine \u2014 "
    "your laptop, a VPS, or a cloud server.\n\n"
    "<b>How it powers your bot:</b>\n"
    "1. User sends a message to your Telegram bot\n"
    "2. Bot sends it to Extella running on your machine\n"
    "3. Extella runs the AI expert (image processor, transcriber, etc.)\n"
    "4. Result is sent back to your user automatically\n\n"
    "Supports Mac, Windows, Linux \u2014 or any server with Docker."
)
_DEPLOY_PROMPT = (
    "Deploy an Extella execution server on my machine.\n"
    "Requirements: internet-accessible server with Docker installed.\n"
    "Steps:\n"
    "1. Pull and run the Extella server Docker container\n"
    "2. Configure it to accept API requests on port 7755\n"
    "3. Generate an API token\n"
    "4. Return: server URL and API token\n\n"
    "Please handle the full deployment automatically."
)


def _is_local(name: str, desc: str = "") -> bool:
    if name.lower() in _KNOWN_LOCAL:
        return True
    t = (name + " " + (desc or "")).lower()
    return any(w in t for w in _LOCAL_SIGNALS)


def _clean_desc(desc: str) -> str:
    for sep in [". Parameters:", "\nParameters:", " Parameters:"]:
        if sep in desc:
            desc = desc.split(sep)[0]
            break
    return desc.strip()[:110]


def _detect_prompt_param(name: str, desc: str) -> str:
    n = name.lower()
    if "translat" in n: return "text"
    if any(k in n for k in ("image","photo","background")): return "image_url"
    return "prompt"


def _build_expert_kb(exps: list, selected: set, bot_id: int) -> dict:
    rows = []
    for exp in exps:
        name = exp["name"]
        local = _is_local(name, exp.get("description", ""))
        badge = "\U0001f4bb" if local else "\u2601\ufe0f"
        check = "\u2705" if name in selected else "\u25fb\ufe0f"
        label = _clean_desc(exp.get("description", name))
        if len(label) > 36: label = label[:36] + "..."
        rows.append([{"text": f"{check}{badge} {label}", "callback_data": f"exp|{name}|{bot_id}"}])
    if selected:
        rows.append([{"text": "\U0001f680 Continue \u2192", "callback_data": f"activate|{bot_id}"}])
    else:
        rows.append([{"text": "\u261d\ufe0f Select at least 1", "callback_data": "noop"}])
    rows.append([{"text": "\U0001f504 Search again", "callback_data": f"research|{bot_id}"}])
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
        elif text == "/connect": await _cmd_connect_device(cid, u, s)
        elif text == "/cancel":
            u.state = "start"; u.pending_bot_id = None; u.pending_key_name = None
            await s.flush()
            await motherbot.send_message(cid, "Cancelled. Use /start to begin again.")
        elif u.state == "waiting_token": await _handle_token(cid, text, u, s)
        elif u.state == "waiting_feature_description": await _handle_desc(cid, text, u, s)
        elif u.state == "waiting_extella_token": await _handle_extella_token(cid, text, u, s)
        elif u.state == "waiting_server_url": await _handle_server_url(cid, text, u, s)
        elif u.state == "waiting_server_token": await _handle_server_token(cid, text, u, s)
        elif u.state == "waiting_api_key_input": await _handle_api_key_input(cid, text, u, s)
        else: await motherbot.send_message(cid, "Use /start or /mybots")


async def _cmd_start(cid, u, s):
    u.state = "waiting_token"; u.pending_bot_id = None; u.pending_key_name = None
    await s.flush()
    await motherbot.send_message(cid,
        "\U0001f44b Welcome to <b>Extella Motherbot</b>!\n\n"
        "I help you build smart Telegram bots powered by AI experts \u2014 "
        "no coding required.\n\n"
        "\U0001f9e0 <b>How it works:</b>\n"
        "1. Create a bot on Telegram (@BotFather)\n"
        "2. Describe what your bot should do\n"
        "3. I find matching AI experts from Extella library\n"
        "4. Quick runtime setup (\u22482 min)\n"
        "5. Your bot is live! \U0001f389\n\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "\U0001f4cb <b>Send me your bot token from @BotFather:</b>")


async def _handle_token(cid, text, u, s):
    if not TOKEN_RE.match(text):
        await motherbot.send_message(cid,
            "\u274c That doesn't look like a bot token.\n"
            "Format: <code>1234567890:AABBcc...</code>\n\n"
            "Get it from @BotFather using /token command.")
        return
    await motherbot.send_message(cid, "\u23f3 Verifying token...")
    gm = await TelegramClient(text).get_me()
    if not gm.get("ok"):
        await motherbot.send_message(cid,
            f"\u274c Invalid token.\n<code>{gm.get('description','?')}</code>\n\nPlease try again.")
        return
    bi = gm["result"]; th = token_to_hash(text)
    dup = (await s.execute(select(Bot).where(Bot.token_hash == th))).scalar_one_or_none()
    if dup:
        await motherbot.send_message(cid,
            f"\u26a0\ufe0f Bot @{bi['username']} already registered. Use /mybots.")
        return
    bot = Bot(user_telegram_id=u.telegram_id,
              token_encrypted=encrypt_token(text, settings.secret_key),
              token_hash=th, bot_telegram_id=bi["id"],
              bot_name=bi["first_name"], bot_username=bi.get("username"), is_active=False)
    s.add(bot); await s.flush()
    u.state = "waiting_feature_description"; u.pending_bot_id = bot.id; await s.flush()
    await motherbot.send_message(cid,
        f"\u2705 <b>Bot @{bi.get('username')} connected!</b>\n\n"
        "Now describe <b>what your bot should do</b>.\n"
        "I'll search Extella's expert library for the best matches.\n\n"
        "\U0001f4dd <b>Examples:</b>\n"
        "\u2022 <i>translate text to different languages</i>\n"
        "\u2022 <i>answer customer questions with AI</i>\n"
        "\u2022 <i>generate social media posts</i>\n"
        "\u2022 <i>remove background from photos</i>\n"
        "\u2022 <i>transcribe voice messages to text</i>\n"
        "\u2022 <i>summarize web pages and articles</i>\n\n"
        "\u270d\ufe0f <b>Describe your bot's purpose:</b>")


async def _handle_desc(cid, text, u, s):
    bid = u.pending_bot_id
    if not bid: await motherbot.send_message(cid, "/start"); return
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if not bot: await motherbot.send_message(cid, "/start"); return
    await motherbot.send_message(cid,
        f"\U0001f50d Searching Extella library for <i>{text[:60]}</i>...")
    matches = await extella.search_experts(text, limit=7)
    if not matches:
        await motherbot.send_message(cid,
            "\U0001f615 No experts found. Try rephrasing:\n"
            "\u2022 <i>AI assistant chatbot</i>\n"
            "\u2022 <i>image processing</i>\n"
            "\u2022 <i>text translation</i>\n"
            "\u2022 <i>voice transcription</i>")
        return
    bot.system_prompt = text
    await s.execute(delete(BotExpert).where(BotExpert.bot_id == bid))
    local_count = 0
    for i, m in enumerate(matches):
        desc = m.get("description", m["name"])
        is_loc = _is_local(m["name"], desc)
        if is_loc: local_count += 1
        s.add(BotExpert(bot_id=bid, expert_name=m["name"],
                        display_name=_clean_desc(desc),
                        exec_type="local" if is_loc else "cloud",
                        params_json={"__prompt_param__": _detect_prompt_param(m["name"], desc)},
                        is_active=True, sort_order=i))
    await s.flush()
    u.state = "choosing_experts"; await s.flush()
    selected = {m["name"] for m in matches}
    exps_dicts = [{"name": m["name"], "description": m.get("description","")} for m in matches]
    if local_count > 0:
        legend = (f"\n\n\u2601\ufe0f = Extella cloud  \U0001f4bb = requires your device "
                  f"({local_count} expert{'s' if local_count > 1 else ''})")
    else:
        legend = "\n\n\u2601\ufe0f All experts run on Extella cloud \u2014 no device setup needed!"
    await motherbot.send_message(cid,
        f"\U0001f3af <b>Found {len(matches)} experts</b>{legend}\n\n"
        "All selected \u2705 \u2014 tap to deselect.\nReady? Press <b>\U0001f680 Continue</b>",
        reply_markup=_build_expert_kb(exps_dicts, selected, bid))


async def _show_execution_mode(cid, u, s, bot, exps):
    cloud_exps = [e for e in exps if e.exec_type == "cloud"]
    local_exps = [e for e in exps if e.exec_type == "local"]
    if not local_exps:
        await _do_activate(cid, u, s, bot, exps); return
    local_names = ", ".join(f"<code>{e.expert_name}</code>" for e in local_exps[:3])
    cloud_note = ""
    if cloud_exps:
        cloud_note = (f"\n\n\u2601\ufe0f <b>{len(cloud_exps)} cloud expert(s)</b> run on "
                      "Extella servers automatically \u2014 no setup needed for those.")
    u.state = "choosing_execution_mode"; u.pending_bot_id = bot.id; await s.flush()
    await motherbot.send_message(cid,
        "\u2699\ufe0f <b>Runtime Setup Required</b>\n\n"
        f"These experts need a local machine to run:\n{local_names}\n\n"
        "They use libraries (image processors, AI models, file tools) "
        f"that require a real machine environment.{cloud_note}\n\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        + _EXTELLA_ABOUT +
        "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
        "Choose how to run your bot:",
        reply_markup={"inline_keyboard": [
            [{"text": "\U0001f5a5 Option A \u2014 My Computer (Recommended)",
              "callback_data": f"mode_desktop|{bot.id}"}],
            [{"text": "\U0001f310 Option B \u2014 My Own Server",
              "callback_data": f"mode_server|{bot.id}"}],
            [{"text": "\u2601\ufe0f Option C \u2014 Cloud only (skip local experts)",
              "callback_data": f"mode_cloud_only|{bot.id}"}],
        ]})


async def _send_desktop_instructions(cid):
    await motherbot.send_message(cid,
        "\U0001f5a5\ufe0f <b>Connect Extella Desktop</b>\n\n"
        + _EXTELLA_ABOUT + "\n\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "<b>Setup (\u22482 minutes):</b>\n\n"
        "<b>1.</b> Download Extella Desktop:\n"
        "   <a href=\"https://extella.ai/download\">extella.ai/download</a>\n"
        "   (Mac / Windows / Linux)\n\n"
        "<b>2.</b> Install and launch Extella Desktop\n\n"
        "<b>3.</b> In Extella: <b>Settings \u2192 API Tokens \u2192 Generate</b>\n\n"
        "<b>4.</b> Copy the token and send it here:\n\n"
        "\U0001f4e4 <b>Paste your Extella API token:</b>",
        reply_markup={"inline_keyboard": [[
            {"text": "\u2753 What is Extella?", "callback_data": "explain_extella"},
            {"text": "\u274c Cancel", "callback_data": "cancel_key"},
        ]]})


async def _handle_extella_token(cid, text, u, s):
    if text.lower() in ("cancel", "/cancel"):
        u.state = "active"; await s.flush()
        await motherbot.send_message(cid, "Cancelled."); return
    await motherbot.send_message(cid, "\u23f3 Validating token and detecting devices...")
    tmp = ExtellaClient(text)
    if not await tmp.validate_token(text):
        await motherbot.send_message(cid,
            "\u274c Invalid token. Please check and try again, "
            "or generate a new one in Extella Desktop \u2192 Settings \u2192 API Tokens.")
        return
    targets = await tmp.list_targets(text)
    if not targets:
        await motherbot.send_message(cid,
            "\u26a0\ufe0f Token is valid but no devices found.\n\n"
            "Make sure Extella Desktop is <b>open and running</b> on your machine "
            "(it registers automatically when running).\n\nThen try sending the token again.")
        return
    first = targets[0]
    target_id = first.get("target") or first.get("id", "")
    target_name = first.get("description", "My Device")[:50]
    bid = u.pending_bot_id
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if bot:
        bot.user_extella_token_enc = encrypt_token(text, settings.secret_key)
        bot.user_target_id = target_id
    u.state = "active"; u.pending_bot_id = None; await s.flush()
    exps = (await s.execute(select(BotExpert).where(
        BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
    await motherbot.send_message(cid,
        f"\u2705 <b>Device connected: {target_name}</b>\n\n"
        "\U0001f4bb Local experts will now run on your machine.\n"
        "Keep Extella Desktop running for the bot to work.")
    await _do_activate(cid, u, s, bot, list(exps))


async def _handle_server_url(cid, text, u, s):
    url = text.strip().rstrip("/")
    if not url.startswith(("http://","https://")):
        await motherbot.send_message(cid,
            "\u274c Please enter a valid URL starting with http:// or https://"); return
    u.pending_key_name = url; u.state = "waiting_server_token"; await s.flush()
    await motherbot.send_message(cid,
        f"\u2705 URL saved: <code>{url}</code>\n\n"
        "Now send the <b>API token</b> generated during deployment:")


async def _handle_server_token(cid, text, u, s):
    server_url = u.pending_key_name or ""
    bid = u.pending_bot_id
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if bot:
        bot.user_extella_token_enc = encrypt_token(text.strip(), settings.secret_key)
        bot.user_target_id = server_url
    u.state = "active"; u.pending_bot_id = None; u.pending_key_name = None; await s.flush()
    exps = (await s.execute(select(BotExpert).where(
        BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
    await motherbot.send_message(cid,
        f"\u2705 <b>Server connected!</b>\n\nServer: <code>{server_url}</code>\n"
        "\U0001f310 Local experts will run on your server.")
    await _do_activate(cid, u, s, bot, list(exps))


async def _handle_api_key_input(cid, text, u, s):
    bid = u.pending_bot_id
    if not bid: await motherbot.send_message(cid, "/start"); return
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if not bot: await motherbot.send_message(cid, "/start"); return
    if text.lower() in ("cancel","/cancel"):
        u.state = "active"; await s.flush()
        await motherbot.send_message(cid, "Cancelled."); return
    if ":" in text:
        parts = text.split(":",1)
        key_name = parts[0].strip().lower().replace(" ","_")
        key_value = parts[1].strip()
    elif u.pending_key_name:
        key_name = u.pending_key_name; key_value = text.strip()
    else:
        await motherbot.send_message(cid, "\u274c Use format: <code>key_name: value</code>"); return
    set_bot_key(bot, key_name, key_value, settings.secret_key)
    await s.flush()
    await motherbot.send_message(cid, f"\u2705 Key <b>{key_name}</b> saved!")
    u.state = "active"; u.pending_key_name = None; await s.flush()


async def _do_activate(cid, u, s, bot, exps, message_id=None):
    raw = decrypt_token(bot.token_encrypted, settings.secret_key)
    wh = await TelegramClient(raw).set_webhook(
        f"{settings.railway_url}/bot/{bot.token_hash}/webhook")
    if not wh.get("ok"):
        await motherbot.send_message(cid,
            f"\u274c Webhook error: {wh.get('description','?')}"); return
    bot.webhook_url = f"{settings.railway_url}/bot/{bot.token_hash}/webhook"
    bot.is_active = True; u.state = "active"
    u.pending_bot_id = None; u.pending_key_name = None; await s.flush()
    lines = []
    for e in exps:
        tag = "\u2601\ufe0f" if e.exec_type == "cloud" else "\U0001f4bb"
        lines.append(f"{tag} <b>{e.expert_name}</b>\n   {(e.display_name or '')[:70]}")
    local_n = sum(1 for e in exps if e.exec_type == "local")
    connect_note = ""
    if local_n and not bot.user_target_id:
        connect_note = (f"\n\n\u26a0\ufe0f <b>{local_n} expert(s) need a device (\U0001f4bb)</b>\n"
                        "Use /connect to set up Extella Desktop or server.")
    text_msg = (f"\U0001f389 <b>@{bot.bot_username} is now live!</b>\n\n"
                f"<b>Active experts ({len(exps)}):</b>\n\n"
                + "\n\n".join(lines) + connect_note +
                f"\n\n\U0001f916 @{bot.bot_username} is ready for users!\n\n"
                "Commands:\n\u2022 /apikeys \u2014 add API keys\n"
                "\u2022 /connect \u2014 link device/server\n"
                "\u2022 /mybots \u2014 manage your bots")
    if message_id:
        await motherbot.edit_message_text(cid, message_id, text_msg)
    else:
        await motherbot.send_message(cid, text_msg)


async def _cmd_mybots(cid, u, s):
    bots = (await s.execute(select(Bot).where(
        Bot.user_telegram_id == u.telegram_id))).scalars().all()
    if not bots:
        await motherbot.send_message(cid, "You have no bots yet. Use /start to create one."); return
    rows = [[{"text": f"{'✅' if b.is_active else '⏸'} @{b.bot_username or '?'} \u2014 {b.bot_name or '?'}",
              "callback_data": f"manage|{b.id}"}] for b in bots]
    rows.append([{"text": "\u2795 Add new bot", "callback_data": "newbot"}])
    await motherbot.send_message(cid, f"\U0001f916 Your bots ({len(bots)}):",
                                 reply_markup={"inline_keyboard": rows})


async def _cmd_apikeys(cid, u, s):
    bots = (await s.execute(select(Bot).where(
        Bot.user_telegram_id == u.telegram_id, Bot.is_active == True))).scalars().all()
    if not bots:
        await motherbot.send_message(cid, "No active bots. Use /start first."); return
    bot = bots[-1]
    u.state = "waiting_api_key_input"; u.pending_bot_id = bot.id; await s.flush()
    existing = get_bot_keys(bot, settings.secret_key)
    await motherbot.send_message(cid,
        f"\U0001f511 <b>API keys for @{bot.bot_username}</b>\n\n"
        f"Stored: {', '.join(existing.keys()) if existing else 'none'}\n\n"
        "Send in format: <code>key_name: value</code>\n\n"
        "Examples:\n"
        "\u2022 <code>fal_api_key: aafd713e-...</code>\n"
        "\u2022 <code>anthropic_api_key: sk-ant-...</code>\n"
        "\u2022 <code>replicate_api_token: r8_...</code>",
        reply_markup={"inline_keyboard": [[
            {"text": "\u25c0\ufe0f Cancel", "callback_data": "cancel_key"}]]})


async def _cmd_connect_device(cid, u, s):
    bots = (await s.execute(select(Bot).where(
        Bot.user_telegram_id == u.telegram_id, Bot.is_active == True))).scalars().all()
    if not bots:
        await motherbot.send_message(cid, "No active bots. Use /start first."); return
    u.state = "waiting_extella_token"; u.pending_bot_id = bots[-1].id; await s.flush()
    await _send_desktop_instructions(cid)


async def _handle_callback(cb: dict):
    cbid = cb["id"]; cid = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]; data = cb.get("data","")
    fu = cb["from"]; tid = fu["id"]
    if data == "noop":
        await motherbot.answer_callback_query(cbid, "Select at least 1!"); return
    if data == "cancel_key":
        await motherbot.answer_callback_query(cbid)
        async with get_session() as s:
            u = await _get_or_create_user(s, tid, fu.get("username"), fu.get("first_name"))
            u.state = "active"; u.pending_key_name = None; await s.flush()
        await motherbot.send_message(cid, "Cancelled."); return
    if data == "newbot":
        async with get_session() as s:
            u = await _get_or_create_user(s, tid, fu.get("username"), fu.get("first_name"))
            await _cmd_start(cid, u, s)
        await motherbot.answer_callback_query(cbid); return
    if data == "explain_extella":
        await motherbot.answer_callback_query(cbid)
        await motherbot.send_message(cid,
            _EXTELLA_ABOUT + "\n\n"
            "\U0001f4e5 Download: <a href=\"https://extella.ai/download\">extella.ai/download</a>\n"
            "\u23f1 Setup time: \u22482 minutes")
        return
    parts = data.split("|"); action = parts[0] if parts else ""
    async with get_session() as s:
        u = await _get_or_create_user(s, tid, fu.get("username"), fu.get("first_name"))
        if action == "exp" and len(parts) == 3:
            ename, bid = parts[1], int(parts[2])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, "Not found"); return
            ex = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.expert_name == ename))).scalar_one_or_none()
            if ex:
                ex.is_active = not ex.is_active; await s.flush()
                await motherbot.answer_callback_query(
                    cbid, "\u2705 Added" if ex.is_active else "\u25fb\ufe0f Removed")
            all_e = (await s.execute(select(BotExpert).where(BotExpert.bot_id == bid)
                .order_by(BotExpert.sort_order))).scalars().all()
            sel_set = {e.expert_name for e in all_e if e.is_active}
            ed = [{"name": e.expert_name,"description": e.display_name or ""} for e in all_e]
            await motherbot.edit_message_text(cid, mid,
                f"Selected: <b>{len(sel_set)}</b> of {len(all_e)}\nTap to toggle.",
                reply_markup=_build_expert_kb(ed, sel_set, bid))
        elif action == "research" and len(parts) == 2:
            bid = int(parts[1]); u.state = "waiting_feature_description"
            u.pending_bot_id = bid; await s.flush()
            await motherbot.answer_callback_query(cbid)
            await motherbot.send_message(cid, "\u270d\ufe0f Describe again:")
        elif action == "activate" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, "Not found"); return
            exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
            if not exps:
                await motherbot.answer_callback_query(cbid, "Select at least 1!", show_alert=True); return
            await motherbot.answer_callback_query(cbid, "\u23f3 Setting up...")
            await _show_execution_mode(cid, u, s, bot, list(exps))
        elif action == "mode_desktop" and len(parts) == 2:
            bid = int(parts[1]); u.state = "waiting_extella_token"
            u.pending_bot_id = bid; await s.flush()
            await motherbot.answer_callback_query(cbid)
            await _send_desktop_instructions(cid)
        elif action == "mode_server" and len(parts) == 2:
            bid = int(parts[1]); u.state = "waiting_server_url"
            u.pending_bot_id = bid; await s.flush()
            await motherbot.answer_callback_query(cbid)
            await motherbot.send_message(cid,
                "\U0001f310 <b>Self-Hosted Server Setup</b>\n\n"
                "You need a VPS/cloud server with Docker installed.\n\n"
                "<b>Step 1.</b> Open Extella Desktop \u2192 AI Agent and send this message:\n\n"
                "<pre>" + _DEPLOY_PROMPT + "</pre>\n\n"
                "<b>Step 2.</b> Wait for deployment to complete.\n\n"
                "<b>Step 3.</b> Send me the server URL:\n"
                "Example: <code>http://123.45.67.89:7755</code>")
        elif action == "mode_cloud_only" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot: await motherbot.answer_callback_query(cbid, "Not found"); return
            cloud_exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.exec_type == "cloud",
                BotExpert.is_active == True))).scalars().all()
            if not cloud_exps:
                await motherbot.answer_callback_query(
                    cbid, "No cloud experts selected!", show_alert=True); return
            await motherbot.answer_callback_query(cbid, "\u23f3 Activating...")
            await _do_activate(cid, u, s, bot, list(cloud_exps))
        elif action == "manage" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot: await motherbot.answer_callback_query(cbid, "Not found"); return
            exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
            fl = "\n".join(
                f"{'☁️' if e.exec_type=='cloud' else '💻'} {e.display_name or e.expert_name}"
                for e in exps) or "\u2014"
            keys = get_bot_keys(bot, settings.secret_key)
            rows = [
                [{"text": "\U0001f511 Manage API Keys", "callback_data": f"manage_keys|{bid}"}],
                [{"text": "\U0001f517 Connect Device/Server",
                  "callback_data": f"mode_desktop|{bid}"}],
                [{"text": "\U0001f5d1 Deactivate", "callback_data": f"deactivate|{bid}"}],
            ]
            await motherbot.answer_callback_query(cbid)
            await motherbot.send_message(cid,
                f"\U0001f916 @{bot.bot_username} | {'✅ Active' if bot.is_active else '⏸ Inactive'}\n\n"
                f"<b>Experts:</b>\n{fl}\n\n"
                f"<b>API Keys:</b> {', '.join(keys.keys()) if keys else 'none'}\n"
                f"<b>Device:</b> {'✅ connected' if bot.user_target_id else '❌ not connected (/connect)'}",
                reply_markup={"inline_keyboard": rows})
        elif action == "manage_keys" and len(parts) == 2:
            bid = int(parts[1]); u.state = "waiting_api_key_input"
            u.pending_bot_id = bid; u.pending_key_name = None; await s.flush()
            await motherbot.answer_callback_query(cbid)
            await motherbot.send_message(cid,
                "\U0001f511 Send key: <code>name: value</code>",
                reply_markup={"inline_keyboard": [[
                    {"text": "\u25c0\ufe0f Cancel", "callback_data": "cancel_key"}]]})
        elif action == "deactivate" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if bot and bot.user_telegram_id == tid:
                raw = decrypt_token(bot.token_encrypted, settings.secret_key)
                await TelegramClient(raw).delete_webhook()
                bot.is_active = False; await s.flush()
                await motherbot.answer_callback_query(cbid, "Deactivated")
                await motherbot.send_message(cid, f"\u23f8\ufe0f @{bot.bot_username} deactivated.")
            else: await motherbot.answer_callback_query(cbid, "Not found")
        else: await motherbot.answer_callback_query(cbid)
