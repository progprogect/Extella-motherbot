"""
Universal per-bot API key manager.

PRINCIPLE:
  - Platform provides OPENAI_API_KEY (injected as api_key + openai_api_key).
  - Users provide any other 3rd-party keys they need.
  - ALL user keys are injected into EVERY expert call.
  - Experts pick whatever they need from params — no hardcoded mapping.
"""
import json
from .crypto import encrypt_token, decrypt_token


def get_bot_keys(bot, secret_key: str) -> dict:
    """Decrypt and return all user-provided API keys for a bot."""
    if not bot.user_api_keys_enc:
        return {}
    try:
        return json.loads(decrypt_token(bot.user_api_keys_enc, secret_key))
    except Exception:
        return {}


def set_bot_key(bot, key_name: str, key_value: str, secret_key: str) -> None:
    """Add or update a single key in bot's encrypted key store."""
    existing = get_bot_keys(bot, secret_key)
    existing[key_name] = key_value
    bot.user_api_keys_enc = encrypt_token(json.dumps(existing), secret_key)


def build_expert_params(bot, secret_key: str, openai_api_key: str) -> dict:
    """
    Build base params injected into EVERY expert call.
    Expert picks whatever keys it needs from this dict.
    Merge order: platform defaults < user-provided (user overrides platform).
    """
    user_keys = get_bot_keys(bot, secret_key)
    base = {
        "api_key":         openai_api_key,
        "openai_api_key":  openai_api_key,
    }
    base.update(user_keys)   # user-provided keys override platform defaults
    return base
