"""
Motherbot v12 — Local-Only Execution

ROUTING DECISION (single path, no redundancy):
  1 expert  → skip routing, call directly
  N experts + OpenAI key → orchestrator v2 (preset concept context)
  N experts, no key     → semantic fallback

EXECUTION: always local (user token + target device).
  No device configured → needs_device response.
  No serverless fallback.

API calls per message:
  Orchestrator path: N×get_kwargs (parallel) + 1×orchestrator + 1×expert-local = 3 round-trips
  Semantic path:     1×search + 1×get_kwargs + 1×expert-local = 3 round-trips
"""
import asyncio
import json
import logging
import re
import time
import ast as _ast
from sqlalchemy import select

from .database import Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import decrypt_token
from .config import settings
from .key_manager import build_expert_params
from .preset_manager import fetch_concept_text
from .mb_bot_manage import (
    handle_delete_confirm as _mgr_del,
    handle_edit_description as _mgr_edit,
)

logger = logging.getLogger(__name__)

# In-memory TTL cache for preset concepts: {(bot_id, concept_id): (text, expires_at)}
_CONCEPT_CACHE: dict[tuple[int, int], tuple[str, float]] = {}
_CONCEPT_TTL = 300  # 5 minutes


async def _get_cached_concept(bot_id: int, concept_id: int, user_tok: str) -> str:
    """Return concept text from cache or fetch from Extella (TTL=5 min)."""
    key = (bot_id, concept_id)
    cached = _CONCEPT_CACHE.get(key)
    if cached and time.monotonic() < cached[1]:
        return cached[0]
    text = await fetch_concept_text(concept_id, user_tok) or ""
    if text:
        _CONCEPT_CACHE[key] = (text, time.monotonic() + _CONCEPT_TTL)
    return text


extella = ExtellaClient(settings.extella_token,
                        profile_id="default",
                        agent_id="agent_extella_default")

_KEY_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35,}"
    r"|eyJ[A-Za-z0-9_.-]{30,}|aafd[A-Za-z0-9_-]{25,}"
    r"|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"
)
_LANG = {
    "ru": "Respond only in Russian.", "en": "Respond only in English.",
    "de": "Respond only in German.", "fr": "Respond only in French.",
    "es": "Respond only in Spanish.", "uk": "Respond only in Ukrainian.",
    "it": "Respond only in Italian.", "pt": "Respond only in Portuguese.",
    "zh": "Respond only in Chinese.", "ja": "Respond only in Japanese.",
    "ko": "Respond only in Korean.", "tr": "Respond only in Turkish.",
    "pl": "Respond only in Polish.", "ar": "Respond only in Arabic.",
}
_DEFAULT_INTENT = {
    "photo": "process this image", "video": "process this video",
    "voice": "transcribe this voice message", "audio": "transcribe this audio",
    "document": "process this document",
}
_CHAT_ACTION = {
    "text": "typing", "photo": "upload_photo", "video": "upload_video",
    "voice": "record_voice", "audio": "upload_voice", "document": "upload_document",
}
_MEDIA_HINT = {
    "photo": "image photo visual processing enhance quality",
    "video": "video processing analyze describe",
    "voice": "voice audio transcription speech to text",
    "audio": "audio transcription processing",
    "document": "document file text extraction analysis",
}
_HIDDEN = {
    "execution_log", "task_id", "Kwargs", "kwargs", "expert_name",
    "api_key", "openai_api_key", "fal_api_key", "anthropic_api_key",
    "replicate_api_token", "groq_api_key", "language", "system_prompt",
    "__prompt_param__", "__tg_bot_token__", "__tg_chat_id__",
    "__railway_callback_url__", "status",
}


def _safe(t: str) -> str:
    return _KEY_RE.sub("[***]", str(t))


def _detect_lang(msg: dict) -> str:
    lang = (msg.get("from") or {}).get("language_code", "")
    if lang: return lang[:2].lower()
    text = msg.get("text", "") or msg.get("caption", "")
    if text:
        cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
        if cyr / max(len(text), 1) > 0.3: return "ru"
    return "en"


def _extract_text(inner) -> str:
    if isinstance(inner, str): return _safe(inner[:4000])
    if not isinstance(inner, dict): return _safe(str(inner)[:500])
    for k in ("answer","translated","post","transcription","summary",
              "text","content","output","message","result","data"):
        v = inner.get(k)
        if v and isinstance(v, str) and len(v.strip()) > 5:
            if len(v) == 36 and v.count("-") == 4: continue
            return _safe(v[:4000])
    op = inner.get("output_path", "")
    if op and inner.get("status") == "success":
        return "\u2705 File saved on your device:\n<code>" + op + "</code>"
    parts = [v for k, v in inner.items()
             if k not in _HIDDEN and isinstance(v, str)
             and 5 < len(v) < 500 and not _KEY_RE.search(v)
             and not (len(v) == 36 and v.count("-") == 4)]
    return _safe(parts[0][:4000]) if parts else "\u2705 Done."


async def handle_user_bot_update(token_hash: str, data: dict):
    try:
        async with get_session() as session:
            bot = (await session.execute(
                select(Bot).where(Bot.token_hash == token_hash,
                                  Bot.is_active == True)
            )).scalar_one_or_none()
            if not bot:
                logger.warning("No active bot for hash=%s", token_hash)
                return
            raw = decrypt_token(bot.token_encrypted, settings.secret_key)
            utg = TelegramClient(raw)
            if msg := data.get("message"):
                await _process(utg, bot, msg, session)
            elif cb := data.get("callback_query"):
                await utg.answer_callback_query(cb["id"])
    except Exception as e:
        logger.error("user_bot hash=%s: %s", token_hash, e, exc_info=True)


async def _process(utg, bot, msg: dict, session):
    """Main message handler. Single routing path, no redundancy."""
    cid = msg["chat"]["id"]
    raw_text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()

    mt, fid = "text", None
    if msg.get("photo"):
        mt, fid = "photo", msg["photo"][-1]["file_id"]
    elif msg.get("video"):
        mt, fid = "video", msg["video"]["file_id"]
    elif msg.get("voice"):
        mt, fid = "voice", msg["voice"]["file_id"]
    elif msg.get("audio"):
        mt, fid = "audio", msg["audio"]["file_id"]
    elif msg.get("document"):
        mt, fid = "document", msg["document"]["file_id"]

    text = caption or raw_text
    if not text and mt != "text":
        text = _DEFAULT_INTENT[mt]
    if not text:
        return
    lang = _detect_lang(msg)

    exps = list((await session.execute(
        select(BotExpert)
        .where(BotExpert.bot_id == bot.id, BotExpert.is_active == True)
        .order_by(BotExpert.sort_order)
    )).scalars().all())

    # /start /help
    if raw_text in ("/start", "/help"):
        if exps:
            lines = "\n".join(f"\u2022 {e.display_name or e.expert_name}" for e in exps)
            await utg.send_message(cid,
                f"\U0001f44b Powered by <b>Extella AI</b>\n\n"
                f"<b>Functions ({len(exps)}):</b>\n{lines}\n\n"
                "Send text, photo, voice or file!")
        else:
            await utg.send_message(cid, "\U0001f44b Bot is being set up.")
        return

    if not exps:
        await utg.send_message(cid, "Bot not configured yet.")
        return

    # Resolve file URL
    furl = None
    if fid:
        furl = await utg.get_file_url(fid)
        if not furl:
            await utg.send_message(cid, "\u26a0\ufe0f Could not load file.")
            return

    await utg.send_chat_action(cid, _CHAT_ACTION.get(mt, "typing"))

    # ── SINGLE ROUTING DECISION ──────────────────────────────────────
    best, params = await _route_and_build(bot, exps, text, mt, furl, lang, cid)

    # ── EXECUTION ────────────────────────────────────────────────────
    result = await _execute(bot, best, params)

    # Handle needs_device (no token/target configured at all)
    if isinstance(result, dict) and result.get("status") == "needs_device":
        exp_name = result.get("expert_name", best.display_name or best.expert_name)
        await utg.send_message(cid,
            f"\u26a0\ufe0f <b>Device not connected</b>\n\n"
            f"Expert <b>{exp_name}</b> runs locally via Extella Desktop.\n\n"
            "Please connect your device: send /connect to the Motherbot.")
        return

    # Handle device_offline (target registered but Extella Desktop not running)
    if isinstance(result, dict) and result.get("status") == "device_offline":
        exp_name = result.get("expert_name", best.display_name or best.expert_name)
        await utg.send_message(cid,
            f"\U0001f4bb <b>Your device is offline</b>\n\n"
            f"Expert <b>{exp_name}</b> needs Extella Desktop running.\n\n"
            "\u2022 Open Extella Desktop on your computer\n"
            "\u2022 Make sure it's connected to the internet\n"
            "\u2022 Then retry your message")
        return

    # ── RESPONSE ─────────────────────────────────────────────────────
    await _respond(utg, cid, result, len(exps) > 1, best.expert_name)


async def _route_and_build(bot, exps, text, mt, furl, lang, cid):
    """
    Single routing decision. Returns (best_expert, params).
    Paths:
      - 1 expert       → direct (no routing, 1× get_kwargs)
      - OpenAI key     → orchestrator (N× get_kwargs parallel + 1× orchestrator)
      - fallback       → semantic search (1× search + 1× get_kwargs)
    Each path makes exactly the minimum API calls needed.
    """
    # ── Path 1: single expert ─────────────────────────────────────
    if len(exps) == 1:
        best = exps[0]
        allowed = await extella.get_expert_kwargs(best.expert_name)
        params = _build_params(bot, best, text, mt, furl, lang, cid, allowed)
        logger.info("[ROUTE] direct: %s", best.expert_name)
        return best, params

    # ── Path 2: orchestrator ──────────────────────────────────────
    openai_key = getattr(settings, "openai_api_key", "")
    if openai_key:
        result = await _try_orchestrator(bot, exps, text, mt, furl, lang, cid, openai_key)
        if result is not None:
            logger.info("[ROUTE] orchestrated: %s", result[0].expert_name)
            return result

    # ── Path 3: semantic fallback ─────────────────────────────────
    query = f"{text} {_MEDIA_HINT.get(mt, "")}".strip()
    best = await _semantic_route(exps, query)
    allowed = await extella.get_expert_kwargs(best.expert_name)
    params = _build_params(bot, best, text, mt, furl, lang, cid, allowed)
    logger.info("[ROUTE] semantic: %s", best.expert_name)
    return best, params


async def _try_orchestrator(bot, exps, text, mt, furl, lang, cid, openai_key):
    """
    Try mb_orchestrator_v2 with preset concept context.
    Fetches all expert kwargs IN PARALLEL (asyncio.gather).
    Returns (best, params) on success, None on timeout/error.
    """
    try:
        # Parallel kwargs fetch — one round-trip for all experts
        kwargs_results = await asyncio.gather(
            *[extella.get_expert_kwargs(e.expert_name) for e in exps],
            return_exceptions=True
        )

        expert_kwargs_map = {}
        exp_info = []
        for e, kw in zip(exps, kwargs_results):
            kw_set = kw if isinstance(kw, set) else set()
            expert_kwargs_map[e.expert_name] = kw_set
            # Exclude internal params from orchestrator context
            public = {k: "" for k in kw_set if not k.startswith("__")}
            exp_info.append({
                "name": e.expert_name,
                "description": e.display_name or e.expert_name,
                "kwargs": public,
            })

        # Fetch preset concept text (cached, TTL 5 min)
        preset_concept_text = ""
        if bot.preset_concept_id and bot.user_extella_token_enc:
            user_tok_for_concept = decrypt_token(bot.user_extella_token_enc, settings.secret_key)
            preset_concept_text = await _get_cached_concept(
                bot.id, bot.preset_concept_id, user_tok_for_concept
            )

        # Call orchestrator v2 with preset context (15s timeout)
        orch_resp = await extella.run_expert("mb_orchestrator_v2", {
            "user_message": text,
            "experts_json": json.dumps(exp_info),
            "api_key": openai_key,
            "media_type": mt,
            "file_url": furl or "",
            "language": lang,
            "preset_concept_text": preset_concept_text,
        }, wait=True, timeout=20)

        # Parse response (Extella returns result as string or dict)
        inner = orch_resp.get("result", {})
        if isinstance(inner, str):
            try: inner = json.loads(inner)
            except Exception:
                try: inner = _ast.literal_eval(inner)
                except Exception: inner = {}

        if not isinstance(inner, dict) or inner.get("status") != "success":
            return None

        orch_name = inner.get("expert_name", "")
        best = next((e for e in exps if e.expert_name == orch_name), None)
        if not best:
            logger.warning("[ORCH] expert %r not in bot list", orch_name)
            return None

        # Build params from orchestrator extraction
        raw_tok = decrypt_token(bot.token_encrypted, settings.secret_key)
        params = dict(inner.get("params", {}))

        # Add internal delivery params
        _INT = {"__tg_bot_token__", "__tg_chat_id__", "__railway_callback_url__"}
        params["__tg_bot_token__"] = raw_tok
        params["__tg_chat_id__"] = str(cid)
        if settings.railway_url:
            params["__railway_callback_url__"] = (
                f"{settings.railway_url}/expert_result/{bot.token_hash}/{cid}"
            )

        # Inject user API keys (filtered to expert signature)
        allowed = expert_kwargs_map.get(best.expert_name, set())
        all_keys = build_expert_params(bot, settings.secret_key, settings.openai_api_key)
        for k, v in all_keys.items():
            if k in allowed:
                params[k] = v

        # Strip anything expert doesnt accept (prevent TypeError)
        if allowed:
            params = {k: v for k, v in params.items()
                      if k in allowed or k in _INT}

        logger.info("[ORCH v2] %s → %s | conf=%.2f | %s",
                    text[:40], best.expert_name,
                    float(inner.get("confidence", 0)),
                    inner.get("reasoning", "")[:60])
        return best, params

    except Exception as e:
        logger.warning("[ORCH] fallback to semantic: %s", e)
        return None


async def _semantic_route(exps: list, query: str):
    """Semantic routing: exact → strong-word (7+ chars) → multi-word → first."""
    if len(exps) == 1: return exps[0]
    try:
        ms = await extella.search_experts(query, limit=15)
        by = {e.expert_name: e for e in exps}
        for m in ms:
            if m["name"] in by:
                logger.info("Route exact: %s", m["name"])
                return by[m["name"]]
        for m in ms:
            for w in (w for w in m["name"].split("_") if len(w) >= 7):
                for bn, be in by.items():
                    if w in bn:
                        logger.info("Route strong-word: %s", bn)
                        return be
        for m in ms:
            lw = {w for w in m["name"].split("_") if len(w) >= 5}
            for bn, be in by.items():
                bw = {w for w in bn.split("_") if len(w) >= 5}
                if len(lw & bw) >= 2:
                    logger.info("Route multi-word: %s", bn)
                    return be
    except Exception as e:
        logger.warning("Semantic route error: %s", e)
    return exps[0]


def _build_params(bot, best, text: str, mt: str, furl,
                  lang: str, chat_id: int, allowed: set | None = None) -> dict:
    """Build expert params, filtered to declared signature."""
    params = dict(best.params_json or {})
    pp = params.pop("__prompt_param__", "prompt")

    if furl:
        for k in ("image_url","input_path","file_url","video_url","audio_url","input_url"):
            params[k] = furl
        if text != _DEFAULT_INTENT.get(mt, ""):
            params[pp] = text
    else:
        params[pp] = text

    inst = _LANG.get(lang, f"Respond in {lang}.")
    if "system_prompt" in params:
        sp = params.get("system_prompt", "")
        if inst not in sp:
            params["system_prompt"] = f"{sp}\n{inst}".strip()
    params["language"] = lang

    raw_tok = decrypt_token(bot.token_encrypted, settings.secret_key)
    params["__tg_bot_token__"] = raw_tok
    params["__tg_chat_id__"] = str(chat_id)
    _INT = {"__tg_bot_token__", "__tg_chat_id__", "__railway_callback_url__"}
    if settings.railway_url:
        params["__railway_callback_url__"] = (
            f"{settings.railway_url}/expert_result/{bot.token_hash}/{chat_id}"
        )

    all_keys = build_expert_params(bot, settings.secret_key, settings.openai_api_key)
    if allowed:
        for k, v in all_keys.items():
            if k in allowed:
                params[k] = v
        params = {k: v for k, v in params.items() if k in allowed or k in _INT}
    else:
        for k, v in all_keys.items():
            if k not in ("api_key", "openai_api_key"):
                params[k] = v
    return params


async def _execute(bot, best, params: dict) -> dict:
    """Local-only execution via user's Extella token + target device.
    No serverless fallback — stability over convenience.
    """
    if not bot.user_extella_token_enc or not bot.user_target_id:
        logger.warning("[EXEC] no device for bot %s expert %s", bot.id, best.expert_name)
        return {"status": "needs_device",
                "expert_name": best.display_name or best.expert_name}

    user_tok = decrypt_token(bot.user_extella_token_enc, settings.secret_key)
    local_cli = ExtellaClient(user_tok)  # no service-level X-Profile-Id/X-Agent-Id

    for attempt in range(1, 3):
        try:
            result = await local_cli.run_expert(
                best.expert_name, params,
                target=bot.user_target_id, wait=True, timeout=120,
            )
            msg = result.get("message", "")
            # Detect device offline: Extella returns "Target X is unavailable"
            if result.get("status") == "error" and "unavailable" in msg.lower() and "target" in msg.lower():
                logger.warning("[EXEC] device offline for bot %s: %s", bot.id, msg)
                return {"status": "device_offline",
                        "expert_name": best.display_name or best.expert_name}
            if result.get("status") == "error" and attempt < 2:
                logger.warning("[EXEC] attempt %d error, retrying: %s | %s",
                               attempt, best.expert_name, result.get("message", ""))
                continue
            logger.info("[EXEC] local ok: %s (attempt %d)", best.expert_name, attempt)
            return result
        except Exception as e:
            logger.error("[EXEC] attempt %d exception: %s | %s", attempt, best.expert_name, e)
            if attempt >= 2:
                return {"status": "error", "message": str(e)}

    return {"status": "error", "message": "Expert execution failed after retries"}


async def _respond(utg, cid: int, result: dict, multi: bool, name: str):
    """Universal result → Telegram delivery."""
    label = f"\U0001f9e0 <i>{name}</i>\n\n" if multi else ""

    if result.get("status") == "async":
        await utg.send_message(cid,
            f"{label}\u23f3 <b>Running on your device</b>\n\n"
            "Expert is processing locally. Result will appear in ~/Downloads.\n"
            "<i>Keep Extella Desktop running.</i>")
        return

    if result.get("status") == "error":
        await utg.send_message(cid, f"\u26a0\ufe0f {_safe(result.get('message', 'Error'))}")
        return

    inner = result.get("result", result)

    # Parse string result (Extella wraps dict as string repr)
    if isinstance(inner, str):
        try: inner = json.loads(inner)
        except Exception:
            try: inner = _ast.literal_eval(inner)
            except Exception: pass

    if not inner:
        await utg.send_message(cid, label + "No response. Please try again.")
        return

    if isinstance(inner, dict) and inner.get("status") == "error":
        await utg.send_message(cid, f"\u26a0\ufe0f {_safe(inner.get('message', 'Error'))}")
        return

    if isinstance(inner, dict):
        if inner.get("sent_to_telegram"): return

        iu = (inner.get("result_url") or inner.get("image_url")
              or inner.get("output_url") or inner.get("output_image_url"))
        if iu:
            await _send_media(utg, cid, iu, label + inner.get("message", "\u2705"), "photo")
            return

        au = inner.get("audio_url") or inner.get("voice_url") or inner.get("tts_url")
        if au:
            await _send_media(utg, cid, au, label, "voice")
            return

        vu = inner.get("video_url") or inner.get("output_video_url")
        if vu:
            await _send_media(utg, cid, vu, label + "\u2705", "video")
            return

        if inner.get("output_path") and inner.get("status") == "success":
            await utg.send_message(cid,
                f"{label}\u2705 File saved on your device:\n"
                f"\U0001f4c1 <code>{inner['output_path']}</code>")
            return

    await utg.send_message(cid, label + _extract_text(inner))


async def _send_media(utg, cid: int, url: str, caption: str, media_type: str):
    """Send media to Telegram. Tries URL directly, falls back gracefully."""
    import httpx
    size = 0
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            hr = await c.head(url, follow_redirects=True)
            size = int(hr.headers.get("content-length", 0))
    except Exception:
        pass
    mb_size = size / 1024 / 1024

    if media_type == "photo":
        if mb_size > 50:
            await utg.send_message(cid,
                f"{caption}\n\U0001f4ce File {mb_size:.1f}MB \u2014 "
                f'<a href="{url}">Download</a>')
            return
        r = await utg.send_photo(cid, url, caption=caption)
        if r.get("ok"): return
        r2 = await utg.send_document(cid, url, caption=caption)
        if not r2.get("ok"):
            await utg.send_message(cid, f'{caption}\n\U0001f5bc <a href="{url}">Open</a>')

    elif media_type == "voice":
        if mb_size > 50:
            await utg.send_message(cid, f'\U0001f3b5 <a href="{url}">Audio</a>')
            return
        r = await utg.send_voice(cid, url)
        if not r.get("ok"):
            r2 = await utg.send_audio(cid, url, caption=caption)
            if not r2.get("ok"):
                await utg.send_message(cid, f'\U0001f3b5 <a href="{url}">Audio</a>')
        elif caption.strip():
            await utg.send_message(cid, caption.strip())

    elif media_type == "video":
        if mb_size > 50:
            await utg.send_message(cid, f'{caption}\n\U0001f3ac <a href="{url}">Watch</a>')
            return
        r = await utg.send_video(cid, url, caption=caption)
        if not r.get("ok"):
            await utg.send_message(cid, f'{caption}\n\U0001f3ac <a href="{url}">Watch</a>')