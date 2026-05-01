"""
Cloud runners — direct API implementations running on Railway server.
These bypass Extella execution engine entirely.
Each runner returns a standardised dict with result_url / answer / translated / post / transcription.
"""
import logging
import httpx
import io
import asyncio

logger = logging.getLogger(__name__)


async def run_ai_assistant(
    prompt: str = "",
    api_key: str = "",
    system_prompt: str = "You are a helpful AI assistant. Be concise and friendly.",
    model: str = "gpt-4o-mini",
    temperature: float = 0.7,
    max_tokens: int = 1000,
    language: str = "",
    **_,
) -> dict:
    if not prompt:
        return {"status": "error", "message": "prompt is required"}
    if not api_key:
        return {"status": "error", "message": "OpenAI API key not configured"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "temperature": temperature, "max_tokens": max_tokens,
                      "messages": [{"role": "system", "content": system_prompt},
                                   {"role": "user", "content": prompt}]},
            )
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                return {"status": "error", "message": data["error"].get("message", "OpenAI error")}
            answer = data["choices"][0]["message"]["content"].strip()
            return {"status": "success", "answer": answer}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return {"status": "error", "message": "Неверный OpenAI API ключ"}
        if e.response.status_code == 429:
            return {"status": "error", "message": "OpenAI: слишком много запросов. Попробуйте позже."}
        return {"status": "error", "message": f"OpenAI HTTP {e.response.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def run_translate(
    text: str = "",
    target_lang: str = "en",
    api_key: str = "",
    model: str = "gpt-4o-mini",
    language: str = "",
    **_,
) -> dict:
    if not text:
        return {"status": "error", "message": "text is required"}
    if not api_key:
        return {"status": "error", "message": "OpenAI API key not configured"}
    # Normalise language code → full name
    lang_map = {
        "en": "English", "ru": "Russian", "de": "German", "fr": "French",
        "es": "Spanish", "uk": "Ukrainian", "it": "Italian", "pt": "Portuguese",
        "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "tr": "Turkish",
        "pl": "Polish", "ar": "Arabic",
    }
    lang_name = lang_map.get(target_lang.lower(), target_lang)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "temperature": 0.3, "max_tokens": 2000,
                      "messages": [
                          {"role": "system", "content":
                               f"Translate to {lang_name}. Return ONLY the translation, no explanations."},
                          {"role": "user", "content": text}
                      ]},
            )
            r.raise_for_status()
            translated = r.json()["choices"][0]["message"]["content"].strip()
            return {"status": "success", "translated": translated}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def run_content_gen(
    prompt: str = "",
    api_key: str = "",
    platform: str = "telegram",
    tone: str = "engaging",
    language: str = "ru",
    include_hashtags: str = "true",
    **_,
) -> dict:
    if not prompt:
        return {"status": "error", "message": "prompt is required"}
    if not api_key:
        return {"status": "error", "message": "OpenAI API key not configured"}
    lang_map = {"ru": "Russian", "en": "English", "de": "German", "fr": "French", "es": "Spanish"}
    lang_name = lang_map.get(language[:2].lower(), "Russian")
    ht = "Add 3-5 hashtags at the end." if include_hashtags.lower() == "true" else "No hashtags."
    sys_p = (f"You are a social media content creator. Write a {tone} {platform} post in {lang_name}. "
             f"{ht} Return ONLY the post text.")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": "gpt-4o-mini", "temperature": 0.8, "max_tokens": 600,
                      "messages": [{"role": "system", "content": sys_p},
                                   {"role": "user", "content": prompt}]},
            )
            r.raise_for_status()
            post = r.json()["choices"][0]["message"]["content"].strip()
            return {"status": "success", "post": post}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def run_remove_background(
    image_url: str = "",
    fal_api_key: str = "",
    fal_api_key_value: str = "",
    **_,
) -> dict:
    key = fal_api_key or fal_api_key_value
    if not image_url: return {"status": "error", "message": "image_url is required"}
    if not key: return {"status": "error", "message": "FAL_API_KEY not configured. Add at fal.ai"}
    return await _fal_queue("fal-ai/birefnet", key,
                             {"image_url": image_url, "model": "General Use (Light)"})


async def run_image_enhance(
    image_url: str = "",
    fal_api_key: str = "",
    fal_api_key_value: str = "",
    upscaling_factor: int = 4,
    **_,
) -> dict:
    key = fal_api_key or fal_api_key_value
    if not image_url: return {"status": "error", "message": "image_url is required"}
    if not key: return {"status": "error", "message": "FAL_API_KEY not configured. Add at fal.ai"}
    return await _fal_queue("fal-ai/aura-sr", key,
                             {"image_url": image_url, "upscaling_factor": upscaling_factor},
                             fallback_model="fal-ai/clarity-upscaler",
                             fallback_payload={"image_url": image_url, "scale": 2})


async def run_transcribe(
    audio_url: str = "",
    api_key: str = "",
    language: str = "",
    **_,
) -> dict:
    if not audio_url: return {"status": "error", "message": "audio_url is required"}
    if not api_key: return {"status": "error", "message": "OpenAI API key not configured"}
    # Download audio
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            ar = await c.get(audio_url, headers={"User-Agent": "Mozilla/5.0"})
            ar.raise_for_status()
            audio_bytes = ar.content
    except Exception as e:
        return {"status": "error", "message": f"Failed to download audio: {e}"}

    ext = "ogg"
    for fmt in ("mp3","mp4","wav","ogg","m4a","webm","flac"):
        if f".{fmt}" in audio_url.lower(): ext = fmt; break
    mime_map = {"mp3":"audio/mpeg","mp4":"audio/mp4","wav":"audio/wav",
                "ogg":"audio/ogg","m4a":"audio/mp4","webm":"audio/webm","flac":"audio/flac"}

    try:
        async with httpx.AsyncClient(timeout=60) as c:
            files = {"file": (f"audio.{ext}", io.BytesIO(audio_bytes), mime_map.get(ext,"audio/ogg"))}
            data = {"model": "whisper-1", "response_format": "json"}
            if language and language not in ("auto",""):
                data["language"] = language
            wr = await c.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files=files, data=data,
            )
            wr.raise_for_status()
            text = wr.json().get("text","").strip()
            return {"status": "success", "transcription": text,
                    "answer": f"🎤 <b>Транскрипция:</b>\n\n{text}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def _fal_queue(model: str, key: str, payload: dict,
                     fallback_model: str = "", fallback_payload: dict | None = None) -> dict:
    """Generic fal.ai queue runner. Returns dict with result_url or error."""
    import time
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.post(f"https://queue.fal.run/{model}", headers=headers, json=payload)
            r.raise_for_status()
        except Exception as e:
            if fallback_model:
                logger.info(f"fal.ai {model} failed ({e}), trying {fallback_model}")
                try:
                    async with httpx.AsyncClient(timeout=20) as c2:
                        r = await c2.post(f"https://queue.fal.run/{fallback_model}",
                                          headers=headers, json=fallback_payload or payload)
                        r.raise_for_status()
                        model = fallback_model
                except Exception as e2:
                    return {"status": "error", "message": f"fal.ai failed: {e2}"}
            else:
                return {"status": "error", "message": f"fal.ai failed: {e}"}

    job = r.json()
    rid = job.get("request_id")
    if not rid:
        return {"status": "error", "message": f"No request_id from fal.ai: {job}"}

    status_url = f"https://queue.fal.run/{model}/requests/{rid}/status"
    result_url_api = f"https://queue.fal.run/{model}/requests/{rid}"

    for attempt in range(40):
        await asyncio.sleep(3)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                sr = await c.get(status_url, headers=headers)
                sd = sr.json()
                st = sd.get("status","")
                logger.debug(f"fal.ai {model} attempt {attempt+1}: {st}")
                if st == "COMPLETED":
                    async with httpx.AsyncClient(timeout=10) as c2:
                        rr = await c2.get(result_url_api, headers=headers)
                        rd = rr.json()
                    img = rd.get("image") or rd.get("output")
                    if isinstance(img, dict): url = img.get("url") or img.get("href","")
                    elif isinstance(img, list) and img: url = (img[0].get("url","") if isinstance(img[0],dict) else img[0])
                    elif isinstance(img, str): url = img
                    else: url = rd.get("url","") or rd.get("result_url","")
                    if url:
                        return {"status": "success", "result_url": url, "message": "✅ Готово!"}
                    return {"status": "error", "message": f"No URL in result: {rd}"}
                elif st in ("FAILED","ERROR"):
                    return {"status": "error", "message": f"fal.ai processing failed: {sd.get('error',str(sd))}"}
        except Exception as e:
            logger.warning(f"fal.ai poll error: {e}")
    return {"status": "error", "message": "fal.ai timeout (120s)"}


# ── Registry ──────────────────────────────────────────────────────────────────
# Maps expert_name → async runner function
# These run DIRECTLY on Railway — no Extella execution needed
CLOUD_RUNNERS: dict[str, callable] = {
    "mb_ai_assistant":          run_ai_assistant,
    "mb_translate_text":        run_translate,
    "mb_translate":             run_translate,
    "mb_generate_content":      run_content_gen,
    "mb_remove_background_cloud": run_remove_background,
    "mb_remove_bg_cloud":       run_remove_background,
    "mb_image_enhance_cloud":   run_image_enhance,
    "mb_transcribe_voice":      run_transcribe,
    "mb_transcribe_audio":      run_transcribe,
}
