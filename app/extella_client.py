import httpx
import logging
logger = logging.getLogger(__name__)
EXTELLA_BASE = "https://api.extella.ai"

# Dronor v0.5.3 requires X-Profile-Id and X-Agent-Id in ALL requests
_PROFILE_ID = "default"
_AGENT_ID   = "agent_extella_default"


def _headers(token: str) -> dict:
    return {
        "X-Auth-Token":  token,
        "X-Profile-Id":  _PROFILE_ID,
        "X-Agent-Id":    _AGENT_ID,
        "Content-Type":  "application/json",
    }


class ExtellaClient:
    def __init__(self, token: str):
        self.token = token

    def _h(self) -> dict:
        return _headers(self.token)

    async def search_experts(self, query: str, limit: int = 10) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{EXTELLA_BASE}/api/blocks/search",
                    headers=self._h(), json={"query": query, "limit": limit})
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
        if target:
            payload["target"] = target
        try:
            async with httpx.AsyncClient(timeout=timeout + 10) as c:
                r = await c.post(f"{EXTELLA_BASE}/api/expert/run",
                    headers=self._h(), json=payload)
                r.raise_for_status()
                resp = r.json()
            if resp.get("task_id") and not resp.get("result"):
                logger.warning(f"Expert {expert_name} returned task_id (async/device)")
                return {"status": "async", "task_id": resp.get("task_id"),
                        "expert_name": expert_name}
            return resp
        except httpx.TimeoutException:
            return {"status": "error",
                    "message": f"Expert '{expert_name}' timed out ({timeout}s)"}
        except httpx.HTTPStatusError as e:
            return {"status": "error",
                    "message": f"Extella API error: HTTP {e.response.status_code}"}
        except Exception as e:
            logger.error(f"run_expert({expert_name}): {e}")
            return {"status": "error", "message": str(e)}

    async def validate_token(self, token: str) -> bool:
        """Validate an Extella API token (using that token's own headers)."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"{EXTELLA_BASE}/api/token/validate",
                    headers=_headers(token),
                    json={},
                )
                data = r.json()
                return data.get("valid", False) or data.get("status") == "success"
        except Exception:
            return False

    async def list_targets(self, token: str) -> list[dict]:
        """List registered devices/targets for the given token."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"{EXTELLA_BASE}/api/targets/list",
                    headers=_headers(token),
                    json={},
                )
                data = r.json()
                return data.get("results", [])
        except Exception as e:
            logger.error(f"list_targets: {e}")
            return []
