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
import re
import httpx

logger = logging.getLogger(__name__)

EXTELLA_BASE = "https://api.extella.ai"


def _build_concept_text(system_prompt: str, experts: list) -> str:
    """
    Build the Agent Execution Guide concept text for a bot preset.
    Includes EXPERTS: and FLOW: blocks so the agentic router activates for this bot.
    """
    expert_lines = []
    for e in experts:
        if isinstance(e, dict):
            name = e.get("expert_name", "?")
            display = e.get("display_name", "") or name
        else:
            name = getattr(e, "expert_name", "?")
            display = getattr(e, "display_name", "") or name
        expert_lines.append(f"  - {name}: {display[:120]}")

    experts_block = "\n".join(expert_lines) if expert_lines else "  (none configured)"

    return (
        f"AGENT EXECUTION GUIDE\n"
        f"=====================\n"
        f"Bot purpose: {system_prompt[:300]}\n\n"
        f"EXPERTS:\n{experts_block}\n\n"
        f"FLOW:\n"
        f"  1. Analyze user message and identify intent\n"
        f"  2. Select the most relevant expert from EXPERTS list\n"
        f"  3. Extract required parameters from user message\n"
        f"  4. Call the expert with extracted params\n"
        f"  5. If the result needs further processing, call another expert\n"
        f"  6. Return the final result to the user\n\n"
        f"RULES:\n"
        f"  - All experts run locally via Extella Desktop\n"
        f"  - If expert needs api_key, inject from user's saved keys automatically\n"
        f"  - If key is missing, return error so user can provide it\n"
        f"  - Max 6 expert calls per request\n"
        f"  - Always respond in the user's language\n"
        f"  - Be concise and helpful in your final answer"
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


def _concept_score(description: str, concept_text: str) -> float:
    """
    Simple keyword-overlap score between bot description and a concept.
    Returns 0.0–1.0. Concepts with higher PRESET affinity score higher.
    """
    desc_words = set(re.findall(r"\w+", description.lower()))
    if not desc_words:
        return 0.0
    # Extract content from concept text (first 800 chars is usually the key block)
    concept_sample = concept_text[:800].lower()
    concept_words = set(re.findall(r"\w+", concept_sample))
    overlap = len(desc_words & concept_words)
    return overlap / max(len(desc_words), 1)


async def search_concept_templates(
    description: str,
    token: str,
    min_score: float = 0.25,
    top_k: int = 3,
) -> list[dict]:
    """
    Search for PRESET concept templates in the user's Extella account
    that match the given bot description.

    Returns a list (up to top_k) of dicts:
      {concept_id, title, concept_text, score}

    Only concepts that start with "PRESET:" or contain "FLOW:" are considered.
    """
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(
                f"{EXTELLA_BASE}/api/concept/list",
                headers=_api_headers(token),
                json={},
            )
            if r.status_code != 200:
                logger.warning("[PRESET] concept/list status=%s", r.status_code)
                return []
            data = r.json()
            concepts = data.get("results", data.get("concepts", []))
    except Exception as e:
        logger.warning("[PRESET] search_concept_templates error: %s", e)
        return []

    candidates: list[dict] = []
    for item in concepts:
        cid = item.get("concept_id") or item.get("id")
        text = item.get("concept_text") or item.get("text") or ""
        if not text or not cid:
            continue
        # Only consider agent execution guides
        if not (text.startswith("PRESET:") or "FLOW:" in text or "EXPERTS:" in text):
            continue
        score = _concept_score(description, text)
        if score < min_score:
            continue
        # Extract title from first line
        first_line = text.splitlines()[0][:80] if text else ""
        candidates.append({
            "concept_id": int(cid),
            "title": first_line,
            "concept_text": text,
            "score": score,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_k]


async def fetch_concept_text(concept_id: int, user_tok: str) -> str | None:
    """Fetch concept text by ID from user's Extella account."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{EXTELLA_BASE}/api/concept/list",
                headers=_api_headers(user_tok),
                json={},
            )
            if r.status_code == 200:
                data = r.json()
                concepts = data.get("results", data.get("concepts", []))
                for c_item in concepts:
                    if c_item.get("concept_id") == concept_id or c_item.get("id") == concept_id:
                        return c_item.get("concept_text") or c_item.get("text")
    except Exception as e:
        logger.warning("[PRESET] fetch_concept_text error: %s", e)
    return None
