"""
Cloud runners — direct API implementations running on Railway.
Each runner uses only keys passed to it — never reads from settings directly.
Bot-level keys are injected by bot_router via key_manager.get_bot_keys().
"""
import logging, httpx, io, asyncio
logger = logging.getLogger(__name__)


async def run_ai_assistant(
    prompt: str = "",
    api_key: str = "",  # Platform OpenAI key injected by router
    system_prompt: str = "You are a helpful AI assistant. Be concise and friendly.",
    model: str = "gpt-4o-mini",
    temperature: float = 0.7,
    max_tokens: int = 1000,
    language: str = "",
    **_,
) -> dict:
    if not prompt: return {"status": "error", "message": "prompt is required"}
    if not api_key: return {"status": "error", "message": "❌ OpenAI key not configured on this platform"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "temperature": temperature, "max_tokens": max_tokens,
                      "messages": [{"role": "system", "content": system_prompt},
                                   {"role": "user", "content": prompt}]})
            r.raise_for_status()
            data = r.json()
            if "error" in data: return {"status": "error", "message": data["error"].get("message", "OpenAI error")}
            return {"status": "success", "answer": data["choices"][0]["message"]["content"].strip()}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401: return {"status": "error", "message": "Неверный OpenAI API ключ"}
        if e.response.status_code == 429: return {"status": "error", "message": "OpenAI: слишком много запросов. Попробуйте позже."}
        return {"status": "error", "message": f"OpenAI error: {e.response.status_code}"}
    except Exception as e: return {"status": "error", "message": str(e)}


async def run_translate(
    text: str = "", api_key: str = "", target_lang: str = "en",
    model: str = "gpt-4o-mini", language: str = "", **_,
) -> dict:
    if not text: return {"status": "error", "message": "text is required"}
    if not api_key: return {"status": "error", "message": "❌ OpenAI key not configured"}
    lang_map = {"en":"English","ru":"Russian","de":"German","fr":"French","es":"Spanish",
                "uk":"Ukrainian","it":"Italian","pt":"Portuguese","zh":"Chinese",
                "ja":"Japanese","ko":"Korean","tr":"Turkish","pl":"Polish","ar":"Arabic"}
    lang_name = lang_map.get(target_lang.lower(), target_lang)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "temperature": 0.3, "max_tokens": 2000,
                      "messages": [
                          {"role": "system", "content": f"Translate to {lang_name}. Return ONLY the translation."},
                          {"role": "user", "content": text}]})
            r.raise_for_status()
            return {"status": "success", "translated": r.json()["choices"][0]["message"]["content"].strip()}
    except Exception as e: return {"status": "error", "message": str(e)}


async def run_content_gen(
    prompt: str = "", api_key: str = "", platform: str = "telegram",
    tone: str = "engaging", language: str = "ru",
    include_hashtags: str = "true", **_,
) -> dict:
    if not prompt: return {"status": "error", "message": "prompt is required"}
    if not api_key: return {"status": "error", "message": "❌ OpenAI key not configured"}
    lang_map = {"ru":"Russian","en":"English","de":"German","fr":"French","es":"Spanish"}
    lang_name = lang_map.get(language[:2].lower(), "Russian")
    ht = "Add 3-5 hashtags at the end." if include_hashtags.lower() == "true" else "No hashtags."
    sys_p = f"Write a {tone} {platform} post in {lang_name}. {ht} Return ONLY the post."
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "gpt-4o-mini", "temperature": 0.8, "max_tokens": 600,
                      "messages": [{"role": "system", "content": sys_p},
                                   {"role": "user", "content": prompt}]})
            r.raise_for_status()
            return {"status": "success", "post": r.json()["choices"][0]["message"]["content"].strip()}
    except Exception as e: return {"status": "error", "message": str(e)}


async def run_remove_background(
    image_url: str = "", fal_api_key: str = "", **_,
) -> dict:
    if not image_url: return {"status": "error", "message": "image_url is required"}
    if not fal_api_key:
        return {"status": "error", "message":
                "❌ Для удаления фона нужен ключ fal.ai.\n"
                "Добавьте его командой /apikeys"}
    return await _fal_queue("fal-ai/birefnet", fal_api_key,
                             {"image_url": image_url, "model": "General Use (Light)"})


async def run_image_enhance(
    image_url: str = "", fal_api_key: str = "", upscaling_factor: int = 4, **_,
) -> dict:
    if not image_url: return {"status": "error", "message": "image_url is required"}
    if not fal_api_key:
        return {"status": "error", "message":
                "❌ Для улучшения изображений нужен ключ fal.ai.\n"
                "Добавьте его командой /apikeys"}
    return await _fal_queue(
        "fal-ai/aura-sr", fal_api_key,
        {"image_url": image_url, "upscaling_factor": upscaling_factor},
        fallback_model="fal-ai/clarity-upscaler",
        fallback_payload={"image_url": image_url, "scale": 2})


async def run_transcribe(
    audio_url: str = "", api_key: str = "", language: str = "", **_,
) -> dict:
    if not audio_url: return {"status": "error", "message": "audio_url is required"}
    if not api_key: return {"status": "error", "message": "❌ OpenAI key not configured"}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            ar = await c.get(audio_url, headers={"User-Agent": "Mozilla/5.0"})
            ar.raise_for_status()
            audio_bytes = ar.content
    except Exception as e: return {"status": "error", "message": f"Failed to download audio: {e}"}

    ext = "ogg"
    for fmt in ("mp3","mp4","wav","ogg","m4a","webm","flac"):
        if f".{fmt}" in audio_url.lower(): ext = fmt; break
    mime = {"mp3":"audio/mpeg","mp4":"audio/mp4","wav":"audio/wav","ogg":"audio/ogg",
            "m4a":"audio/mp4","webm":"audio/webm","flac":"audio/flac"}.get(ext,"audio/ogg")
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            files = {"file": (f"audio.{ext}", io.BytesIO(audio_bytes), mime)}
            data: dict = {"model": "whisper-1", "response_format": "json"}
            if language and language not in ("auto", ""): data["language"] = language
            wr = await c.post("https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files, data=data)
            wr.raise_for_status()
            text = wr.json().get("text", "").strip()
            return {"status": "success", "transcription": text,
                    "answer": f"🎤 <b>Транскрипция:</b>\n\n{text}"}
    except Exception as e: return {"status": "error", "message": str(e)}


async def _fal_queue(model: str, key: str, payload: dict,
                     fallback_model: str = "", fallback_payload: dict | None = None) -> dict:
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.post(f"https://queue.fal.run/{model}", headers=headers, json=payload)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                err = e.response.json().get("detail","")
                if "balance" in err.lower() or "locked" in err.lower():
                    return {"status": "error",
                            "message": "❌ Недостаточно средств на fal.ai. Пополните баланс на fal.ai/dashboard/billing"}
                return {"status": "error", "message": f"fal.ai: {err[:150]}"}
            if fallback_model:
                async with httpx.AsyncClient(timeout=20) as c2:
                    try:
                        r = await c2.post(f"https://queue.fal.run/{fallback_model}",
                                          headers=headers, json=fallback_payload or payload)
                        r.raise_for_status()
                        model = fallback_model
                    except Exception as e2:
                        return {"status": "error", "message": f"fal.ai failed: {e2}"}
            else:
                return {"status": "error", "message": f"fal.ai HTTP {e.response.status_code}"}
        except Exception as e:
            return {"status": "error", "message": f"fal.ai error: {e}"}

    job = r.json()
    rid = job.get("request_id")
    if not rid: return {"status": "error", "message": f"No request_id: {job}"}
    status_url = f"https://queue.fal.run/{model}/requests/{rid}/status"
    result_url_api = f"https://queue.fal.run/{model}/requests/{rid}"

    for attempt in range(40):
        await asyncio.sleep(3)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                sd = (await c.get(status_url, headers=headers)).json()
                st = sd.get("status", "")
                logger.debug(f"fal.ai {model} attempt {attempt+1}: {st}")
                if st == "COMPLETED":
                    rd = (await (await c.__aenter__()).get(result_url_api, headers=headers)).json()
                    img = rd.get("image") or rd.get("output")
                    if isinstance(img, dict): url = img.get("url","") or img.get("href","")
                    elif isinstance(img, list) and img: url = img[0].get("url","") if isinstance(img[0],dict) else img[0]
                    elif isinstance(img, str): url = img
                    else: url = rd.get("url","") or rd.get("result_url","")
                    if url: return {"status": "success", "result_url": url, "message": "✅ Готово!"}
                    return {"status": "error", "message": f"No URL in result: {rd}"}
                elif st in ("FAILED","ERROR"):
                    return {"status": "error", "message": f"fal.ai failed: {sd.get('error', str(sd))}"}
        except Exception as e: logger.warning(f"fal.ai poll: {e}")
    return {"status": "error", "message": "fal.ai timeout (120s)"}


# Registry: expert_name → runner function
CLOUD_RUNNERS: dict[str, object] = {
    "mb_ai_assistant":            run_ai_assistant,
    "mb_translate_text":          run_translate,
    "mb_translate":               run_translate,
    "mb_generate_content":        run_content_gen,
    "mb_remove_background_cloud": run_remove_background,
    "mb_remove_bg_cloud":         run_remove_background,
    "mb_image_enhance_cloud":     run_image_enhance,
    "mb_transcribe_voice":        run_transcribe,
    "mb_transcribe_audio":        run_transcribe,
}
