import re
import logging
from sqlalchemy import select, delete

from .database import User, Bot, BotExpert, get_session
from .mb_bot_manage import (handle_delete_confirm as _mgr_del, handle_edit_description as _mgr_edit)
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import encrypt_token, token_to_hash, decrypt_token
from .config import settings
from .key_manager import get_bot_keys, set_bot_key, build_expert_params
from .preset_manager import create_or_update_preset_concept, search_concept_templates
from .agentic_router import run_agentic_loop, _is_agent_guide

logger = logging.getLogger(__name__)
TOKEN_RE = re.compile(r"^\d{8,12}:[A-Za-z0-9_-]{35,}$")
motherbot = TelegramClient(settings.motherbot_token)
extella = ExtellaClient(settings.extella_token,
                        profile_id="default",
                        agent_id="agent_extella_default")

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


def _is_local(*_) -> bool:
    """All experts execute locally via Extella Desktop."""
    return True


# Experts that must never appear in user-facing bot configuration
_EXPERT_BLOCKLIST: set[str] = {
    # Personal / launcher utilities
    "launch_conversation_assistant", "write_assistant_script",
    "debug_conversation_assistant", "realtime_conversation_assistant",
    # Low quality / broken
    "temperature_converter", "text_analyzer",
    # OS automation (not for bots)
    "windows_agent", "mac_agent",
    # Internal Telegram utilities
    "send_telegram_message", "telegram_send", "telegram_bot_info",
    "telegram_downloads_search_bot", "mb_get_tg_updates",
    # Internal Motherbot orchestrators & deployment
    "mb_orchestrator", "mb_orchestrator_v2",
    # 3D / highly specialized pipelines
    "generate_3d_pipeline", "generate_3d_model_huggingface",
    # Internal automation & system tools
    "automation_discovery", "design_critique",
    # Voice cloning (requires heavy GPU, too specialized)
    "voice_clone_tortoise", "voice_clone_xtts",
    # Gaming / niche
    "cs2_match_tracker",
}
_EXPERT_BLOCKLIST_PREFIXES = (
    # Motherbot internal
    "mb_test_", "mb_simulate_", "mb_push_", "mb_full_", "mb_add_", "mb_fix_",
    # Twitter Lead Agent (entire suite is internal)
    "tw_",
    # Generic test/debug
    "test_", "debug_",
)

# Descriptions starting with these indicate a broken/low-quality expert
_BAD_DESC_PREFIXES = (
    "the code needs", "the expert code", "the provided code",
    "one sentence", "needs to be", "this expert needs",
)

# Canonical Motherbot experts — shown first, one per semantic slot
_MB_PRIORITY = [
    "mb_ai_assistant", "mb_translate_text", "mb_transcribe_voice",
    "mb_image_generator", "image_generate", "text_to_speech",
    "audio_to_text_free", "code_review_ai",
]


def _is_blocked(name: str, desc: str = "") -> bool:
    """Return True if the expert should be hidden from users."""
    if name in _EXPERT_BLOCKLIST:
        return True
    if any(name.startswith(p) for p in _EXPERT_BLOCKLIST_PREFIXES):
        return True
    if desc and any(desc.lower().startswith(p) for p in _BAD_DESC_PREFIXES):
        return True
    return False


def _dedup_experts(matches: list, limit: int = 7) -> list:
    """Filter blocklisted experts, deduplicate by semantic slot, prefer mb_* experts."""
    seen_slots: dict[str, str] = {}  # slot_key → chosen expert name
    slot_keywords = [
        ("chat",      ("chat", "assistant", "gpt", "openai_chat", "ai_assistant", "mb_ai")),
        ("translate", ("translat",)),
        ("transcribe",("transcri", "whisper", "speech_to_text", "audio_to_text")),
        ("tts",       ("tts", "text_to_speech")),
        ("image",     ("image", "photo", "dall", "stable_diff", "flux", "logo",
                       "generate_outfit", "interior_design", "background")),
        ("video",     ("video",)),
        ("code",      ("code_review", "code_explainer", "code_")),
        ("social",    ("social", "twitter", "instagram", "linkedin")),
        ("content",   ("content", "rewrite", "copywr", "post_gen")),
        ("voice",     ("voice_clone", "clone")),
        ("search",    ("search", "scrape", "crawl", "browse")),
        ("file",      ("file_", "pdf_", "document", "ocr")),
    ]

    def get_slot(name: str) -> str:
        nl = name.lower()
        for slot, kws in slot_keywords:
            if any(k in nl for k in kws):
                return slot
        return name  # unique slot per name = no dedup

    priority_set = set(_MB_PRIORITY)
    # Sort: priority experts first, then by score (already ordered by API)
    sorted_matches = sorted(matches, key=lambda m: (0 if m["name"] in priority_set else 1, matches.index(m)))

    result = []
    for m in sorted_matches:
        name = m["name"]
        desc = m.get("description", "")
        if _is_blocked(name, desc):
            continue
        slot = get_slot(name)
        if slot in seen_slots:
            continue  # already have an expert for this slot
        seen_slots[slot] = name
        result.append(m)
        if len(result) >= limit:
            break
    return result


def _clean_desc(desc: str) -> str:
    """Extract clean first-sentence display name from expert description."""
    # Cut before Parameters/Part-of section
    for sep in [". Parameters:", "\nParameters:", " Parameters:", ". Part of"]:
        if sep in desc:
            desc = desc.split(sep)[0]
            break
    # Take first sentence only
    first = desc.split(".")[0].strip()
    if len(first) >= 12:
        return first[:100]
    return desc.strip()[:100]


def _detect_prompt_param(name: str, desc: str) -> str:
    n = name.lower()
    if "translat" in n: return "text"
    if any(k in n for k in ("image", "photo", "background")): return "image_url"
    return "prompt"


def _build_expert_kb(exps: list, selected: set, bot_id: int) -> dict:
    rows = []
    for exp in exps:
        name = exp["name"]
        check = "\u2705" if name in selected else "\u25fb\ufe0f"
        label = _clean_desc(exp.get("description", name))
        if len(label) > 36: label = label[:36] + "..."
        rows.append([{"text": f"{check}\U0001f4bb {label}", "callback_data": f"exp|{name}|{bot_id}"}])
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
        elif u.state == "waiting_device_uuid": await _handle_device_uuid(cid, text, u, s)
        elif u.state == "waiting_server_url": await _handle_server_url(cid, text, u, s)
        elif u.state == "waiting_server_token": await _handle_server_token(cid, text, u, s)
        elif u.state == "waiting_api_key_input": await _handle_api_key_input(cid, text, u, s)
        elif u.state == 'waiting_delete_confirm':
            await _mgr_del_wrap(cid, text, u, s)
        elif u.state == 'waiting_edit_description':
            await _mgr_edit_wrap(cid, text, u, s)
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

    # Try concept templates first (Agent Execution Guides with FLOW/EXPERTS blocks)
    templates = await search_concept_templates(text, settings.extella_token, min_score=0.3)
    best_tpl = templates[0] if templates else None

    if best_tpl:
        expert_names = _parse_experts_from_concept(best_tpl["concept_text"])
        if expert_names:
            bot.system_prompt = text
            await s.execute(delete(BotExpert).where(BotExpert.bot_id == bid))
            for i, name in enumerate(expert_names):
                s.add(BotExpert(
                    bot_id=bid, expert_name=name,
                    display_name=name.replace("_", " ").title(),
                    exec_type="local",
                    params_json={"__prompt_param__": _detect_prompt_param(name, "")},
                    is_active=True, sort_order=i,
                ))
            await s.flush()
            u.state = "choosing_experts"; await s.flush()
            selected = set(expert_names)
            exps_dicts = [{"name": n, "description": n.replace("_", " ").title()}
                          for n in expert_names]
            title = best_tpl["title"].replace("PRESET:", "").strip()[:50]
            legend = "\n\n\U0001f4bb All experts run locally on your device via Extella"
            await motherbot.send_message(cid,
                f"\u2728 <b>Matched template: {title}</b>{legend}\n\n"
                "All selected \u2705 \u2014 tap to deselect.\nReady? Press <b>\U0001f680 Continue</b>",
                reply_markup=_build_expert_kb(exps_dicts, selected, bid))
            return

    # Fallback: semantic expert search
    raw = await extella.search_experts(text, limit=30)
    matches = _dedup_experts(raw, limit=7)
    if not matches:
        await motherbot.send_message(cid,
            "\U0001f615 No experts found. Try rephrasing:\n"
            "\u2022 <i>AI assistant chatbot</i>\n"
            "\u2022 <i>image generation</i>\n"
            "\u2022 <i>text translation</i>\n"
            "\u2022 <i>voice transcription</i>")
        return
    bot.system_prompt = text
    await s.execute(delete(BotExpert).where(BotExpert.bot_id == bid))
    for i, m in enumerate(matches):
        desc = m.get("description", m["name"])
        s.add(BotExpert(bot_id=bid, expert_name=m["name"],
                        display_name=_clean_desc(desc),
                        exec_type="local",
                        params_json={"__prompt_param__": _detect_prompt_param(m["name"], desc)},
                        is_active=True, sort_order=i))
    await s.flush()
    u.state = "choosing_experts"; await s.flush()
    selected = {m["name"] for m in matches}
    exps_dicts = [{"name": m["name"], "description": m.get("description", "")} for m in matches]
    legend = "\n\n\U0001f4bb All experts run locally on your device via Extella"
    await motherbot.send_message(cid,
        f"\U0001f3af <b>Found {len(matches)} experts</b>{legend}\n\n"
        "All selected \u2705 \u2014 tap to deselect.\nReady? Press <b>\U0001f680 Continue</b>",
        reply_markup=_build_expert_kb(exps_dicts, selected, bid))


async def _show_execution_mode(cid, u, s, bot, exps):
    if bot.user_extella_token_enc and bot.user_target_id:
        await _do_activate(cid, u, s, bot, exps)
        return
    u.state = "choosing_execution_mode"; u.pending_bot_id = bot.id; await s.flush()
    await motherbot.send_message(cid,
        "\u2699\ufe0f <b>Device Setup Required</b>\n\n"
        "All experts run locally on your machine via Extella Desktop.\n\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        + _EXTELLA_ABOUT +
        "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
        "Choose how to connect:",
        reply_markup={"inline_keyboard": [
            [{"text": "\U0001f5a5 Option A \u2014 My Computer (Recommended)",
              "callback_data": f"mode_desktop|{bot.id}"}],
            [{"text": "\U0001f310 Option B \u2014 My Own Server",
              "callback_data": f"mode_server|{bot.id}"}],
        ]})


async def _send_desktop_instructions(cid):
    await motherbot.send_message(
        cid,
        "\U0001f5a5\ufe0f <b>Connect Your Device to Extella</b>\n\n"
        + _EXTELLA_ABOUT + "\n\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "<b>How to get your API Token:</b>\n\n"
        "<b>1.</b> Download and open Extella Desktop:\n"
        "   <a href=\"https://extella.ai/download\">extella.ai/download</a>\n\n"
        "<b>2.</b> In the Extella chat, send this message to the AI agent:\n\n"
        "<code>Generate an API token for me</code>\n\n"
        "<b>3.</b> The agent will reply with a token like:\n"
        "   <code>xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx</code>\n\n"
        "<b>4.</b> Copy that token and paste it here:\n\n"
        "\U0001f4e4 <b>Paste your Extella API Token:</b>",
        reply_markup={"inline_keyboard": [[
            {"text": "\u2753 What is Extella?", "callback_data": "explain_extella"},
            {"text": "\u274c Cancel", "callback_data": "cancel_key"},
        ]]}
    )

async def _handle_extella_token(cid, text, u, s):
    if text.lower() in ("cancel", "/cancel"):
        u.state = "active"; await s.flush()
        await motherbot.send_message(cid, "Cancelled."); return
    await motherbot.send_message(cid, "Validating token...")
    tmp = ExtellaClient(text)
    if not await tmp.validate_token(text):
        await motherbot.send_message(
            cid,
            "Invalid token. Please check and try again.\n\n"
            "Open Extella Desktop, ask the AI agent:\n"
            "<code>Generate an API token for me</code>\n\n"
            "Then paste the UUID token here."
        )
        return
    # Check that the token actually has execute permissions (not just syntactically valid)
    has_exec = await tmp.check_execute_permission(text)
    if not has_exec:
        await motherbot.send_message(
            cid,
            "\u26a0\ufe0f <b>Token accepted, but execution rights are missing</b>\n\n"
            "This token passed basic validation but cannot run experts.\n\n"
            "To get a full-access token:\n"
            "\u2022 Open Extella Desktop\n"
            "\u2022 Ask the AI agent: <code>Generate an API token for me</code>\n"
            "\u2022 Paste the new token here\n\n"
            "Or send /connect later once you have a valid token."
        )
        return
    # Try to find device UUID from targets list
    targets = await tmp.list_targets(text)
    target_id = None
    target_name = "Your Device"
    if targets:
        first = targets[0]
        target_id = first.get("target") or first.get("id")
        target_name = first.get("description", "Your Device")[:50]
    bid = u.pending_bot_id
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if bot:
        bot.user_extella_token_enc = encrypt_token(text, settings.secret_key)
        if target_id:
            # Found device UUID from API
            bot.user_target_id = target_id
        elif not bot.user_target_id or bot.user_target_id == "auto":
            # No device found and no existing UUID — ask for it
            u.state = "waiting_device_uuid"
            u.pending_bot_id = bid
            await s.flush()
            await motherbot.send_message(
                cid,
                "Token saved!\n\n"
                "To link your device, send me your <b>Device UUID</b>.\n\n"
                "Find it in Extella Desktop:\n"
                "Ask the AI agent: <code>What is my device UUID?</code>\n\n"
                "Or check Settings section in Extella Desktop.\n\n"
                "Format: <code>xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx</code>"
            )
            return
        # else: keep existing valid UUID
    u.state = "active"; u.pending_bot_id = None; await s.flush()
    exps = (await s.execute(select(BotExpert).where(
        BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
    device_msg = f"Device: {target_name}" if target_id else "Device linked."
    await motherbot.send_message(
        cid,
        f"Connected! {device_msg}\n\n"
        "Local experts will now run on your machine."
    )
    await _do_activate(cid, u, s, bot, list(exps))

async def _handle_device_uuid(cid, text, u, s):
    """User provides their device UUID manually."""
    if text.lower() in ("cancel", "/cancel"):
        u.state = "active"; await s.flush()
        await motherbot.send_message(cid, "Cancelled."); return
    uuid = text.strip()
    if len(uuid) != 36 or uuid.count("-") != 4:
        await motherbot.send_message(
            cid,
            "That doesn't look like a valid UUID.\n"
            "Format: <code>xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx</code>\n\n"
            "Try again or /cancel"
        )
        return
    bid = u.pending_bot_id
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if bot:
        bot.user_target_id = uuid
    u.state = "active"; u.pending_bot_id = None; await s.flush()
    exps = (await s.execute(select(BotExpert).where(
        BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
    await motherbot.send_message(
        cid,
        f"Device UUID saved: <code>{uuid[:8]}...</code>\n"
        "Local experts will now run on your device!"
    )
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


def _detect_key_name(value: str) -> str | None:
    """Auto-detect API key type from its value format."""
    v = value.strip()
    if v.startswith("sk-proj-") or v.startswith("sk-"):
        return "api_key"
    if v.startswith("aafd") and len(v) > 30:
        return "fal_api_key"
    if v.startswith("sk-ant-"):
        return "anthropic_api_key"
    if v.startswith("r8_"):
        return "replicate_api_token"
    return None


def _parse_experts_from_concept(concept_text: str) -> list:
    """
    Extract expert names from the EXPERTS: block/line in a concept text.
    Handles two formats:
      Inline:  EXPERTS: expert_a, expert_b, expert_c
      Block:   EXPERTS:\n  - expert_a: description\n  - expert_b
    """
    names = []
    in_experts = False
    for line in concept_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("EXPERTS:"):
            # Check for inline format first: EXPERTS: a, b, c
            inline_part = stripped[len("EXPERTS:"):].strip()
            if inline_part:
                for part in inline_part.split(","):
                    name = part.strip().split(":")[0].strip()
                    if name and re.match(r'^[a-z][a-z0-9_]{2,}$', name):
                        names.append(name)
                break  # inline format — done
            in_experts = True
            continue
        if in_experts:
            # Stop at next section header
            if stripped.startswith("FLOW:") or stripped.startswith("RULES:") or stripped.startswith("BOT "):
                break
            if stripped and not stripped.startswith("-") and stripped.endswith(":") and stripped == stripped.upper():
                break
            if stripped.startswith("-"):
                name_part = stripped.lstrip("-").strip().split(":")[0].strip()
                if name_part and re.match(r'^[a-z][a-z0-9_]{2,}$', name_part):
                    names.append(name_part)
    return names


async def _handle_api_key_input(cid, text, u, s):
    bid = u.pending_bot_id
    if not bid: await motherbot.send_message(cid, "/start"); return
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if not bot: await motherbot.send_message(cid, "/start"); return
    if text.lower() in ("cancel","/cancel"):
        u.state = "active"; await s.flush()
        await motherbot.send_message(cid, "Cancelled."); return

    text = text.strip()
    if ":" in text:
        parts = text.split(":", 1)
        key_name = parts[0].strip().lower().replace(" ", "_")
        key_value = parts[1].strip()
    elif u.pending_key_name:
        key_name = u.pending_key_name
        key_value = text
    else:
        # Try auto-detect from value format (user just pasted the key)
        auto_name = _detect_key_name(text)
        if auto_name:
            key_name = auto_name
            key_value = text
        else:
            await motherbot.send_message(
                cid,
                "\u274c Could not detect key type. Please use format:\n"
                "<code>api_key: sk-proj-your-openai-key</code>"
            )
            return

    if not key_value:
        await motherbot.send_message(cid, "\u274c Key value is empty."); return

    set_bot_key(bot, key_name, key_value, settings.secret_key)
    await s.flush()
    await motherbot.send_message(
        cid,
        f"\u2705 Key <b>{key_name}</b> saved!\n\n"
        f"Your bot will now use this key for experts that require it."
    )
    u.state = "active"; u.pending_key_name = None; await s.flush()

    # Replay pending agentic message if user was mid-conversation in their bot
    if u.pending_agent_message and u.pending_agent_bot_id:
        await _replay_pending_agent_message(cid, u, s)


async def _replay_pending_agent_message(motherbot_cid: int, u, s) -> None:
    """Re-run the pending agentic message after the user provided a missing key."""
    from .bot_router import _get_cached_concept

    pending_text = u.pending_agent_message
    agent_bot_id = u.pending_agent_bot_id

    # Clear pending state first to avoid infinite replay loops
    u.pending_agent_message = None
    u.pending_agent_key_name = None
    u.pending_agent_bot_id = None
    await s.flush()

    try:
        bot = (await s.execute(select(Bot).where(Bot.id == agent_bot_id))).scalar_one_or_none()
        if not bot or not bot.is_active:
            return

        if not bot.preset_concept_id or not bot.user_extella_token_enc:
            return

        user_tok = decrypt_token(bot.user_extella_token_enc, settings.secret_key)
        concept_text = await _get_cached_concept(bot.id, bot.preset_concept_id, user_tok)
        if not _is_agent_guide(concept_text):
            return

        exps = list((await s.execute(
            select(BotExpert).where(BotExpert.bot_id == bot.id, BotExpert.is_active == True)
            .order_by(BotExpert.sort_order)
        )).scalars().all())
        if not exps:
            return

        # The child bot sends the reply to the user's Telegram ID (same as DM cid)
        child_bot_cid = u.telegram_id
        raw_bot_token = decrypt_token(bot.token_encrypted, settings.secret_key)
        child_utg = TelegramClient(raw_bot_token)

        await child_utg.send_chat_action(child_bot_cid, "typing")

        all_keys = build_expert_params(bot, settings.secret_key, settings.openai_api_key)
        openai_key = (all_keys.get("api_key") or all_keys.get("openai_api_key")
                      or settings.openai_api_key)

        result = await run_agentic_loop(
            bot=bot,
            user_message=pending_text,
            concept_text=concept_text,
            experts=exps,
            user_tok=user_tok,
            target_id=bot.user_target_id or "",
            openai_key=openai_key,
            all_keys=all_keys,
        )

        from .bot_router import _safe as _route_safe
        if result.get("status") == "ok":
            await child_utg.send_message(child_bot_cid, _route_safe(result.get("text", "\u2705 Done.")))
        elif result.get("status") in ("needs_device", "device_offline"):
            await child_utg.send_message(child_bot_cid,
                "\U0001f4bb Your device is offline. Please open Extella Desktop and retry.")
        elif result.get("status") == "needs_key":
            await child_utg.send_message(child_bot_cid,
                f"\U0001f511 Another key is needed: "
                f"<code>{result.get('key_name', 'api_key')}</code>\n"
                "Use /apikeys in the Motherbot to add it.")
        else:
            await child_utg.send_message(child_bot_cid,
                f"\u26a0\ufe0f {_route_safe(result.get('message', 'Error replaying your request.'))}")

    except Exception as e:
        logger.warning("[REPLAY] failed: %s", e)



async def _mgr_del_wrap(cid, text, u, s):
    bid = u.pending_bot_id
    if not bid: await motherbot.send_message(cid, '/start'); return
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if not bot: await motherbot.send_message(cid, '/start'); return
    await _mgr_del(cid, text, u, s, bot, motherbot, extella,
        TelegramClient, decrypt_token, BotExpert, settings)

async def _mgr_edit_wrap(cid, text, u, s):
    bid = u.pending_bot_id
    if not bid: await motherbot.send_message(cid, '/start'); return
    bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
    if not bot: await motherbot.send_message(cid, '/start'); return
    await _mgr_edit(cid, text, u, s, bot, motherbot, extella,
        BotExpert, _is_local, _clean_desc, _detect_prompt_param, _build_expert_kb)

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

    if bot.user_extella_token_enc:
        user_tok = decrypt_token(bot.user_extella_token_enc, settings.secret_key)
        concept_id = await create_or_update_preset_concept(bot, exps, user_tok)
        if concept_id:
            bot.preset_concept_id = concept_id
            await s.flush()
    lines = []
    for e in exps:
        lines.append(f"\U0001f4bb <b>{e.expert_name}</b>\n   {(e.display_name or '')[:70]}")
    connect_note = ""
    if not bot.user_target_id:
        connect_note = (
            "\n\n\u26a0\ufe0f <b>Device not connected!</b>\n"
            "Use /connect to link Extella Desktop \u2014 required to run experts."
        )
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
    stored_str = ", ".join(f"<code>{k}</code>" for k in existing.keys()) if existing else "none"
    await motherbot.send_message(cid,
        f"\U0001f511 <b>API keys for @{bot.bot_username}</b>\n\n"
        f"Currently saved: {stored_str}\n\n"
        "You can simply <b>paste your key</b> — I'll detect the type automatically.\n"
        "Or use format: <code>key_name: value</code>\n\n"
        "<b>Supported key types:</b>\n"
        "\u2022 <b>OpenAI</b>: paste <code>sk-proj-...</code> or <code>api_key: sk-proj-...</code>\n"
        "\u2022 <b>Fal.ai</b>: paste <code>aafd...</code> or <code>fal_api_key: aafd...</code>\n"
        "\u2022 <b>Anthropic</b>: <code>anthropic_api_key: sk-ant-...</code>\n"
        "\u2022 <b>Replicate</b>: <code>replicate_api_token: r8_...</code>\n"
        "\u2022 <b>Any other</b>: <code>my_key_name: value</code>",
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
                "<b>Step 1.</b> Download and install Extella Desktop on any machine:\n"
                "<a href=\"https://extella.ai/download\">extella.ai/download</a>\n"
                "(Mac / Windows / Linux)\n\n"
                "<b>Step 2.</b> Open Extella Desktop \u2192 AI Agent and send this message:\n\n"
                "<pre>" + _DEPLOY_PROMPT + "</pre>\n\n"
                "<b>Step 3.</b> Wait for deployment to complete (Extella will handle it).\n\n"
                "<b>Step 4.</b> Send me the server URL:\n"
                "Example: <code>http://123.45.67.89:7755</code>")

        elif action == "manage" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot: await motherbot.answer_callback_query(cbid, "Not found"); return
            exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
            fl = "\n".join(
                f"\U0001f4bb {e.display_name or e.expert_name}"
                for e in exps) or "\u2014"
            keys = get_bot_keys(bot, settings.secret_key)
            rows = [
                [{"text": "✏️ Edit Functions", "callback_data": f"edit|{bid}"}],
                [{"text": "\U0001f511 Manage API Keys", "callback_data": f"manage_keys|{bid}"}],
                [{"text": "\U0001f517 Connect Device/Server",
                  "callback_data": f"mode_desktop|{bid}"}],
                [{"text": "\u23f8\ufe0f Deactivate", "callback_data": f"deactivate|{bid}"}],
                [{"text": "\U0001f5d1\ufe0f Delete Bot", "callback_data": f"delete_bot|{bid}"}],
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
        elif action == "edit" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, 'Not found'); return
            u.state = 'waiting_edit_description'; u.pending_bot_id = bid
            await s.flush(); await motherbot.answer_callback_query(cbid)
            exps = (await s.execute(select(BotExpert).where(
                BotExpert.bot_id == bid, BotExpert.is_active == True))).scalars().all()
            cur = ', '.join(e.display_name or e.expert_name for e in exps[:3])
            em = '✏️ <b>Edit @' + bot.bot_username + '</b>' + chr(10) + chr(10)
            em += 'Current: <i>' + cur + '</i>' + chr(10) + chr(10)
            em += 'Describe new functionality:' + chr(10) + '✍️ <b>What should your bot do now?</b>'
            await motherbot.send_message(cid, em)

        elif action == "delete_bot" and len(parts) == 2:
            bid = int(parts[1])
            bot = (await s.execute(select(Bot).where(Bot.id == bid))).scalar_one_or_none()
            if not bot or bot.user_telegram_id != tid:
                await motherbot.answer_callback_query(cbid, 'Not found'); return
            u.state = 'waiting_delete_confirm'; u.pending_bot_id = bid
            await s.flush(); await motherbot.answer_callback_query(cbid)
            dm = '🗑 <b>Delete @' + bot.bot_username + '?</b>' + chr(10) + chr(10)
            dm += 'Removes all settings and webhook.' + chr(10)
            dm += 'You can recreate it with the same token later.' + chr(10) + chr(10)
            dm += 'Type <b>yes, delete</b> to confirm:'
            await motherbot.send_message(cid, dm,
                reply_markup={'inline_keyboard': [[
                    {'text': '❌ Cancel', 'callback_data': 'cancel_key'}
                ]]})

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