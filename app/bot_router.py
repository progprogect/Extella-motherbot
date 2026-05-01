import logging
import re
from sqlalchemy import select
from .database import Bot, BotExpert, get_session
from .telegram_client import TelegramClient
from .extella_client import ExtellaClient
from .crypto import decrypt_token
from .config import settings

logger = logging.getLogger(__name__)
extella = ExtellaClient(settings.extella_token)

_KEY_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}"
    r"|AIza[A-Za-z0-9_-]{35,}"
    r"|eyJ[A-Za-z0-9_.-]{30,}"
    r"|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
    r"|aafd[A-Za-z0-9_-]{20,})"
)

LANG_PROMPTS = {
    "ru": "Отвечай только на русском языке.",
    "en": "Respond only in English.",
    "de": "Antworte nur auf Deutsch.",
    "fr": "Reponds uniquement en francais.",
    "es": "Responde solo en espanol.",
    "uk": "Відповідай тільки українською.",
    "it": "Rispondi solo in italiano.",
    "pt": "Responda apenas em portugues.",
    "zh": "只用中文回答。",
    "ja": "日本語のみで回答してください。",
    "ko": "한국어로만 답변하세요.",
    "tr": "Sadece Turkce yanit ver.",
    "pl": "Odpowiadaj tylko po polsku.",
    "ar": "أجب باللغة العربية فقط.",
}

_DEFAULT_INTENT = {
    "photo":    "обработай это изображение",
    "video":    "опиши это видео",
    "voice":    "транскрибируй голосовое",
    "audio":    "транскрибируй аудиофайл",
    "document": "обработай документ",
}
_CHAT_ACTION = {
    "text": "typing", "photo": "upload_photo", "video": "upload_video",
    "voice": "record_voice", "audio": "upload_voice", "document": "upload_document",
}
_MEDIA_HINT = {
    "photo":    "image photo visual processing",
    "video":    "video processing",
    "voice":    "voice audio transcription speech to text",
    "audio":    "audio transcription processing",
    "document": "document file text extraction",
}
# Keys to NEVER show users
_HIDDEN_KEYS = {
    "execution_log", "task_id", "Kwargs", "kwargs", "expert_name",
    "api_key", "fal_api_key", "fal_api_key_value", "language",
    "system_prompt", "__prompt_param__",
}


def _detect_lang(msg: dict) -> str:
    lang = (msg.get("from") or {}).get("language_code", "")
    if lang: return lang[:2].lower()
    text = msg.get("text", "") or msg.get("caption", "")
    if text:
        cyr = sum(1 for c in text if "\u0400" <= c <= "\u04FF")
        if len(text) > 0 and cyr / len(text) > 0.3: return "ru"
    return "en"


def _inject_lang(params: dict, lang: str) -> dict:
    inst = LANG_PROMPTS.get(lang, f"Respond in {lang} language.")
    if "system_prompt" in params:
        sp = params.get("system_prompt", "")
        if inst not in sp:
            params["system_prompt"] = f"{sp}\n{inst}".strip()
    if "language" not in params:
        params["language"] = lang
    return params


def _safe_text(text: str) -> str:
    return _KEY_RE.sub("[***]", text)


def _txt(inner) -> str:
    if isinstance(inner, str):
        return _safe_text(inner[:4000])
    if not isinstance(inner, dict):
        return _safe_text(str(inner)[:500])

    # Known meaningful text fields
    for k in ("answer", "translated", "post", "transcription",
              "text", "content", "output", "message", "result"):
        v = inner.get(k)
        if v and isinstance(v, str) and len(v.strip()) > 5:
            # Skip UUID-like strings
            if len(v) == 36 and v.count("-") == 4: continue
            return _safe_text(v[:4000])

    # output_path — file saved locally
    if inner.get("output_path"):
        path = inner["output_path"]
        return f"✅ Готово!\n📁 Файл сохранён: <code>{path}</code>"

    # Collect safe human-readable values
    parts = []
    for k, v in inner.items():
        if k in _HIDDEN_KEYS: continue
        if k == "status": continue
        if not isinstance(v, str): continue
        if len(v) < 3 or len(v) > 1000: continue
        if _KEY_RE.search(v): continue
        if len(v) == 36 and v.count("-") == 4: continue
        parts.append(f"{k}: {v}")

    if parts:
        return _safe_text("\n".join(parts)[:2000])

    return "✅ Задача выполнена."


async def handle_user_bot_update(token_hash: str, data: dict):
    try:
        async with get_session() as session:
            bot = (await session.execute(
                select(Bot).where(Bot.token_hash == token_hash, Bot.is_active == True)
            )).scalar_one_or_none()
            if not bot:
                logger.warning(f"No bot hash={token_hash}")
                return
            raw = decrypt_token(bot.token_encrypted, settings.secret_key)
            utg = TelegramClient(raw)
            if msg := data.get("message"):
                await _process(utg, bot, msg, session)
            elif cb := data.get("callback_query"):
                await utg.answer_callback_query(cb["id"])
    except Exception as e:
        logger.error(f"user_bot hash={token_hash}: {e}", exc_info=True)


async def _process(utg, bot, msg: dict, session):
    cid = msg["chat"]["id"]
    raw_text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()

    mt = "text"; fid = None
    if msg.get("photo"):    mt = "photo";    fid = msg["photo"][-1]["file_id"]
    elif msg.get("video"):  mt = "video";    fid = msg["video"]["file_id"]
    elif msg.get("voice"):  mt = "voice";    fid = msg["voice"]["file_id"]
    elif msg.get("audio"):  mt = "audio";    fid = msg["audio"]["file_id"]
    elif msg.get("document"): mt = "document"; fid = msg["document"]["file_id"]

    text = caption or raw_text
    if not text and mt != "text":
        text = _DEFAULT_INTENT[mt]
    if not text:
        return

    lang = _detect_lang(msg)

    exps = (await session.execute(
        select(BotExpert)
        .where(BotExpert.bot_id == bot.id, BotExpert.is_active == True)
        .order_by(BotExpert.sort_order)
    )).scalars().all()

    if raw_text in ("/start", "/help"):
        if exps:
            lines = "\n".join(
                f"{'☁️' if e.exec_type == 'cloud' else '💻'} "
                f"{e.display_name or e.expert_name}"
                for e in exps
            )
            local_n = sum(1 for e in exps if e.exec_type == "local")
            conn = ""
            if local_n and not bot.user_target_id:
                conn = (f"\n\n⚠️ {local_n} эксп. работают на твоём ПК "
                        "(💻). /connect чтобы подключить.")
            await utg.send_message(
                cid,
                f"👋 Extella AI | {len(exps)} функций\n{lines}{conn}\n\n"
                "Просто напиши что нужно!"
            )
        else:
            await utg.send_message(cid, "👋 Бот настраивается.")
        return

    if not exps:
        await utg.send_message(cid, "Бот ещё не настроен.")
        return

    furl = None
    if fid:
        furl = await utg.get_file_url(fid)
        if not furl:
            await utg.send_message(cid, "⚠️ Не удалось загрузить файл.")
            return

    await utg.send_chat_action(cid, _CHAT_ACTION.get(mt, "typing"))

    query = f"{text} {_MEDIA_HINT.get(mt, '')}".strip()
    best = await _route(exps, query)
    logger.info(
        f"bot={bot.id} expert={best.expert_name} "
        f"type={mt} lang={lang} exec={best.exec_type}"
    )

    params = dict(best.params_json or {})
    pp = params.pop("__prompt_param__", "prompt")

    if furl:
        uk = {
            "photo": "image_url", "video": "video_url",
            "voice": "audio_url", "audio": "audio_url",
            "document": "file_url",
        }.get(mt, "file_url")
        params[uk] = furl
        if text != _DEFAULT_INTENT.get(mt, "") and pp != uk:
            params[pp] = text
    else:
        params[pp] = text

    if settings.openai_api_key:
        params["api_key"] = settings.openai_api_key
    if settings.fal_api_key:
        params["fal_api_key"] = settings.fal_api_key
        params["fal_api_key_value"] = settings.fal_api_key

    params = _inject_lang(params, lang)

    # ── Cloud vs Local routing ────────────────────────────────────────────────
    etype = best.exec_type or "cloud"

    if etype == "local":
        if not bot.user_target_id or not bot.user_extella_token_enc:
            await utg.send_message(
                cid,
                f"⚠️ <b>{best.expert_name}</b> работает на локальном ПК 💻\n\n"
                "Для работы нужно подключить свой компьютер через /connect"
            )
            return
        utok = decrypt_token(bot.user_extella_token_enc, settings.secret_key)
        client = ExtellaClient(utok)
        result = await client.run_expert(
            best.expert_name, params, timeout=90, target=bot.user_target_id
        )
    else:
        # Serverless: no target in payload
        result = await extella.run_expert(
            best.expert_name, params, timeout=90, target=None
        )

    await _respond(utg, cid, result, len(exps) > 1, best.expert_name)


async def _route(exps: list, query: str):
    if len(exps) == 1:
        return exps[0]
    try:
        ms = await extella.search_experts(query, limit=15)
        by = {e.expert_name: e for e in exps}
        for m in ms:
            if m["name"] in by:
                logger.info(f"Matched {m['name']} score={m.get('score', '?')}")
                return by[m["name"]]
    except Exception as e:
        logger.warning(f"Route fail: {e}")
    return exps[0]


async def _respond(utg, cid: int, result: dict, multi: bool, name: str):
    label = f"🧠 <i>{name}</i>\n\n" if multi else ""

    # ── Status: local task dispatched to user's device ────────────────────────
    if result.get("status") == "local_task":
        await utg.send_message(
            cid,
            f"{label}💻 <b>Задача запущена на твоём устройстве</b>\n\n"
            f"{result.get('message', 'Результат будет сохранён в Downloads.')}\n\n"
            "<i>Убедись что Extella Desktop запущен.</i>"
        )
        return

    # ── Network / client error ────────────────────────────────────────────────
    if result.get("status") == "error":
        msg = _safe_text(result.get("message", "Ошибка"))
        await utg.send_message(cid, f"⚠️ {msg}")
        return

    inner = result.get("result", result)

    if not inner:
        await utg.send_message(cid, label + "Нет ответа. Попробуй снова.")
        return

    if isinstance(inner, dict) and inner.get("status") == "error":
        msg = _safe_text(inner.get("message", "Ошибка"))
        await utg.send_message(cid, f"⚠️ {msg}")
        return

    if isinstance(inner, dict):
        # Image result
        iu = (inner.get("result_url") or inner.get("image_url")
              or inner.get("output_url") or inner.get("output_image_url"))
        if iu:
            cap = label + inner.get("message", "✅")
            r = await utg.send_photo(cid, iu, caption=cap)
            if not r.get("ok"):
                await utg.send_message(
                    cid, f'{label}<a href="{iu}">Открыть изображение</a>')
            return

        # Audio result
        au = inner.get("audio_url") or inner.get("voice_url") or inner.get("tts_url")
        if au:
            await utg.send_voice(cid, au)
            if label:
                await utg.send_message(cid, label.strip())
            return

        # Video result
        vu = inner.get("video_url") or inner.get("output_video_url")
        if vu:
            r = await utg.send_video(cid, vu, caption=label)
            if not r.get("ok"):
                await utg.send_message(
                    cid, f'{label}<a href="{vu}">Открыть видео</a>')
            return

        # Local file result (output_path)
        if inner.get("output_path") and inner.get("status") == "success":
            path = inner["output_path"]
            await utg.send_message(
                cid,
                f"{label}✅ Готово!\n"
                f"📁 Файл сохранён на твоём устройстве:\n"
                f"<code>{path}</code>"
            )
            return

    # Text fallback — guaranteed safe
    await utg.send_message(cid, label + _txt(inner))
