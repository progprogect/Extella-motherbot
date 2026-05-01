"""
Per-bot API key manager.
Keys are stored encrypted as JSON in Bot.user_api_keys_enc.
The platform provides OPENAI_API_KEY for basic AI features.
Users provide their own keys for 3rd-party services (fal.ai, etc.)
"""
import json
from .crypto import encrypt_token, decrypt_token
from .config import settings

# ── Service registry ──────────────────────────────────────────────────────────
# service_id → {display, env_fallback, required_for, instructions}
SERVICES = {
    "fal_api_key": {
        "display": "fal.ai",
        "env_key": None,  # No platform-level fallback — user MUST provide
        "instructions": (
            "Для работы с изображениями (удаление фона, улучшение качества) "
            "нужен бесплатный ключ <b>fal.ai</b>.\n\n"
            "1. Зайдите на <a href=\"https://fal.ai\">fal.ai</a>\n"
            "2. Зарегистрируйтесь (бесплатно)\n"
            "3. Зайдите в Dashboard → API Keys → Create\n"
            "4. Скопируйте ключ и пришлите сюда:"
        ),
        "example": "aafd713e-8d1f-...",
        "skip_text": "Пропустить (функции с изображениями не будут работать)",
    },
    "openai_api_key": {
        "display": "OpenAI",
        "env_key": "OPENAI_API_KEY",  # Platform provides this one
        "instructions": (
            "Для расширенных AI-функций нужен ваш <b>OpenAI API ключ</b>.\n\n"
            "Получите на <a href=\"https://platform.openai.com/api-keys\">platform.openai.com</a>:"
        ),
        "example": "sk-proj-...",
        "skip_text": "Пропустить (будет использован ключ платформы)",
    },
}

# Which cloud runners require which service key
RUNNER_KEYS: dict[str, list[str]] = {
    "mb_ai_assistant":          [],           # Uses platform OpenAI
    "mb_translate_text":        [],           # Uses platform OpenAI
    "mb_generate_content":      [],           # Uses platform OpenAI
    "mb_transcribe_voice":      [],           # Uses platform OpenAI Whisper
    "mb_remove_background_cloud": ["fal_api_key"],
    "mb_remove_bg_cloud":       ["fal_api_key"],
    "mb_image_enhance_cloud":   ["fal_api_key"],
}


def get_required_keys(expert_names: list[str]) -> list[str]:
    """Return list of service keys required by the given experts (deduplicated)."""
    needed = set()
    for name in expert_names:
        for key in RUNNER_KEYS.get(name, []):
            needed.add(key)
    return list(needed)


def get_bot_keys(bot, secret_key: str) -> dict[str, str]:
    """
    Decrypt and return all available API keys for a bot.
    Merges: platform env vars + user-provided keys (user keys take priority).
    """
    keys: dict[str, str] = {}

    # 1. Platform keys (base level)
    for svc_id, svc in SERVICES.items():
        env_key = svc.get("env_key")
        if env_key:
            import os
            val = os.getenv(env_key, "")
            if val:
                keys[svc_id] = val

    # 2. User-provided keys (override platform if present)
    if bot.user_api_keys_enc:
        try:
            user_keys = json.loads(decrypt_token(bot.user_api_keys_enc, secret_key))
            keys.update(user_keys)
        except Exception:
            pass

    return keys


def set_bot_key(bot, key_name: str, key_value: str, secret_key: str) -> None:
    """Add or update a single key in bot's encrypted key store."""
    existing: dict[str, str] = {}
    if bot.user_api_keys_enc:
        try:
            existing = json.loads(decrypt_token(bot.user_api_keys_enc, secret_key))
        except Exception:
            pass
    existing[key_name] = key_value
    bot.user_api_keys_enc = encrypt_token(json.dumps(existing), secret_key)


def has_required_keys(bot, expert_names: list[str], secret_key: str) -> tuple[bool, list[str]]:
    """Check if bot has all required keys. Returns (all_present, missing_list)."""
    required = get_required_keys(expert_names)
    if not required:
        return True, []
    bot_keys = get_bot_keys(bot, secret_key)
    missing = [k for k in required if not bot_keys.get(k)]
    return len(missing) == 0, missing
