import httpx
import logging
logger = logging.getLogger(__name__)
EXTELLA_BASE = "https://api.extella.ai"


def _h(token: str) -> dict:
    """All Extella API requests require these 3 headers (Dronor v0.5.3+)."""
    return {
        "X-Auth-Token":  token,
        "X-Profile-Id":  "default",
        "X-Agent-Id":    "agent_extella_default",
        "Content-Type":  "application/json",
    }


class ExtellaClient:
    def __init__(self, token: str):
        self.token = token

    async def search_experts(self, query: str, limit: int = 10) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{EXTELLA_BASE}/api/blocks/search",
                    headers=_h(self.token),
                    json={"query": query, "limit": limit})
                r.raise_for_status()
                return r.json().get("matches", [])
        except Exception as e:
            logger.error(f"search_experts: {e}")
            return []

    async def run_expert(self, expert_name: str, params: dict | None = None,
                         wait: bool = True, timeout: int = 90,
                         target: str | None = None) -> dict:
        payload: dict = {
            "expert_name": expert_name,
            "params": params or {},
            "wait": wait,
        }
        # Only include target if it's a non-empty string
        if target and isinstance(target, str) and target.strip():
            payload["target"] = target.strip()

        try:
            async with httpx.AsyncClient(timeout=timeout + 10) as c:
                r = await c.post(f"{EXTELLA_BASE}/api/expert/run",
                    headers=_h(self.token), json=payload)
                r.raise_for_status()
                resp = r.json()
            if resp.get("task_id") and not resp.get("result"):
                logger.warning(f"Expert {expert_name} returned task_id (async)")
                return {"status": "async", "task_id": resp.get("task_id"),
                        "expert_name": expert_name}
            return resp
        except httpx.TimeoutException:
            return {"status": "error",
                    "message": f"Expert '{expert_name}' timed out ({timeout}s)"}
        except httpx.HTTPStatusError as e:
            body = ""
            try: body = e.response.text[:200]
            except Exception: pass
            return {"status": "error",
                    "message": f"Extella API HTTP {e.response.status_code}: {body}"}
        except Exception as e:
            logger.error(f"run_expert({expert_name}): {e}")
            return {"status": "error", "message": str(e)}

    async def validate_token(self, token: str) -> bool:
        """
        Validate an Extella API token.
        API expects: POST /api/token/validate with body {"token": "<token>"}
        """
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"{EXTELLA_BASE}/api/token/validate",
                    headers=_h(token),
                    json={"token": token},   # ← token must be in body
                )
                data = r.json()
                return (data.get("valid") is True
                        or data.get("status") == "success")
        except Exception as e:
            logger.error(f"validate_token: {e}")
            return False

    async def list_targets(self, token: str) -> list[dict]:
        """
        List devices registered for the given token's user.
        Returns list of {id, target, description} dicts.
        """
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"{EXTELLA_BASE}/api/targets/list",
                    headers=_h(token),
                    json={},
                )
                data = r.json()
                return data.get("results", [])
        except Exception as e:
            logger.error(f"list_targets: {e}")
            return []
