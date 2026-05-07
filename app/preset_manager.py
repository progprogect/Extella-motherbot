"""
Preset Manager — creates and maintains the Master Concept in Extella.

Architecture:
  Bot (DB) ──→ preset_concept_id
                  │
                  └──→ Extella Concepts DB (user's token):
                         Describes: bot purpose, expert list, routing hints, CSPL types.

The master concept is used by mb_orchestrator_v2 as system context for routing,
so it understands what the bot is supposed to do and can pick the right expert.
"""
import logging
import httpx

logger = logging.getLogger(__name__)

EXTELLA_BASE = "https://api.extella.ai"


def _build_concept_text(system_prompt: str, experts: list) -> str:
    """Build the master concept text for a bot preset."""
    expert_lines = []
    for e in experts:
        name = getattr(e, "expert_name", e.get("expert_name", "?"))
        display = getattr(e, "display_name", e.get("display_name", "")) or name
        expert_lines.append(f"  - {name}: {display[:120]}")

    experts_block = "\n".join(expert_lines) if expert_lines else "  (none configured)"

    return (
        f"MOTHERBOT PRESET\n"
        f"================\n"
        f"Purpose: {system_prompt[:300]}\n\n"
        f"Experts ({len(experts)}):\n{experts_block}\n\n"
        f"Execution: local device via Extella Desktop (cspl=fython / nohup for long tasks)\n"
        f"Routing rule: match user intent to the most relevant expert above.\n"
        f"If unclear — prefer the first expert in the list.\n"
        f"Always return JSON: {{expert_name, params, reasoning}}"
    )


def _api_headers(token: str) -> dict:
    """Standard Extella API headers. X-Profile-Id is required by all concept/expert endpoints."""
    return {
        "X-Auth-Token": token,
        "X-Profile-Id": "default",
        "X-Agent-Id": "agent_extella_default",
        "Content-Type": "application/json",
    }


async def create_or_update_preset_concept(
    bot,
    experts: list,
    user_tok: str,
) -> int | None:
    """Create or update the master concept for a bot preset in Extella.

    Returns the concept_id on success, None on failure.
    Uses the user's own Extella token so the concept lives in their account.
    """
    system_prompt = getattr(bot, "system_prompt", "") or "AI assistant bot"
    concept_text = _build_concept_text(system_prompt, experts)

    headers = _api_headers(user_tok)

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            existing_id = getattr(bot, "preset_concept_id", None)

            if existing_id:
                r = await c.post(
                    f"{EXTELLA_BASE}/api/concept/update",
                    headers=headers,
                    json={"concept_id": existing_id, "new_text": concept_text},
                )
                if r.status_code == 200 and r.json().get("status") == "success":
                    logger.info("[PRESET] updated concept %d for bot %s", existing_id, bot.id)
                    return existing_id
                logger.warning("[PRESET] update failed (%s), creating new", r.status_code)

            r = await c.post(
                f"{EXTELLA_BASE}/api/concept/add",
                headers=headers,
                json={"text": concept_text},
            )
            if r.status_code == 200:
                data = r.json()
                cid = data.get("id") or data.get("concept_id")
                if cid:
                    logger.info("[PRESET] created concept %d for bot %s", cid, bot.id)
                    return int(cid)

            logger.error("[PRESET] concept add failed: %s | %s", r.status_code, r.text[:200])
            return None

    except Exception as e:
        logger.error("[PRESET] exception: %s", e)
        return None


async def fetch_concept_text(concept_id: int, user_tok: str) -> str | None:
    """Fetch concept text by ID from user's Extella account."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{EXTELLA_BASE}/api/concept/list",
                headers=_api_headers(user_tok),
            )
            if r.status_code == 200:
                concepts = r.json().get("concepts", r.json().get("results", []))
                for c_item in concepts:
                    if c_item.get("concept_id") == concept_id or c_item.get("id") == concept_id:
                        return c_item.get("text") or c_item.get("concept_text")
    except Exception as e:
        logger.warning("[PRESET] fetch_concept_text error: %s", e)
    return None
