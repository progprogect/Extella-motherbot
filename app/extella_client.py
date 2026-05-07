import httpx
import logging
logger = logging.getLogger(__name__)
EXTELLA_BASE = "https://api.extella.ai"

def _headers(token: str, profile_id: str | None = None, agent_id: str | None = None) -> dict:
    h = {"X-Auth-Token": token, "Content-Type": "application/json"}
    if profile_id:
        h["X-Profile-Id"] = profile_id
    if agent_id:
        h["X-Agent-Id"] = agent_id
    return h


class ExtellaClient:
    def __init__(self, token: str,
                 profile_id: str | None = None,
                 agent_id: str | None = None):
        self.token = token
        self._profile_id = profile_id
        self._agent_id = agent_id

    def _h(self) -> dict:
        return _headers(self.token, self._profile_id, self._agent_id)

    async def get_expert_kwargs(self, name: str) -> set:
        """Returns set of kwarg names the expert accepts. Empty on error."""
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.post(
                    f"{EXTELLA_BASE}/api/expert/get",
                    headers=self._h(), json={"name": name},
                )
                if r.status_code == 200:
                    params = r.json().get("expert_params", {}) or {}
                    return set(params.keys())
        except Exception as e:
            logger.warning("get_expert_kwargs(%s): %s", name, e)
        return set()

    async def search_experts(self, query: str, limit: int = 15) -> list:
        """Semantic search across Extella expert library."""
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"{EXTELLA_BASE}/api/blocks/search",
                    headers=self._h(),
                    json={"query": query, "limit": limit},
                )
                r.raise_for_status()
                return r.json().get("matches", [])
        except Exception as e:
            logger.error("search_experts: %s", e)
            return []

    async def run_expert(self, expert_name: str, params=None,
                         wait: bool = True, timeout: int = 90, target=None) -> dict:
        payload = {
            "expert_name": expert_name,
            "params": params or {},
            "wait": wait,
        }
        if target:
            payload["target"] = target
        try:
            async with httpx.AsyncClient(timeout=timeout + 10) as c:
                r = await c.post(
                    f"{EXTELLA_BASE}/api/expert/run",
                    headers=self._h(), json=payload,
                )
                r.raise_for_status()
                resp = r.json()
            if resp.get("task_id") and not resp.get("result"):
                return {"status": "async", "task_id": resp.get("task_id"),
                        "expert_name": expert_name}
            return resp
        except httpx.TimeoutException:
            return {"status": "error",
                    "message": f"Expert '{expert_name}' timed out ({timeout}s)"}
        except httpx.HTTPStatusError as e:
            return {"status": "error",
                    "message": f"HTTP {e.response.status_code}"}
        except Exception as e:
            logger.error("run_expert(%s): %s", expert_name, e)
            return {"status": "error", "message": str(e)}

    async def validate_token(self, token: str) -> bool:
        """Validate an Extella API token."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                # Send token both in header AND body (API requires body)
                r = await c.post(
                    f"{EXTELLA_BASE}/api/token/validate",
                    headers=_headers(token),
                    json={"token": token},
                )
                d = r.json()
                return d.get("valid", False) or d.get("status") == "success"
        except Exception:
            return False

    async def list_targets(self, token: str) -> list:
        """List registered devices for the given token."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    f"{EXTELLA_BASE}/api/targets/list",
                    headers=_headers(token),
                    json={},
                )
                return r.json().get("results", [])
        except Exception as e:
            logger.error("list_targets: %s", e)
            return []