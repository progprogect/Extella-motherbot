import httpx, logging
logger = logging.getLogger(__name__)
EXTELLA_BASE = "https://api.extella.ai"

class ExtellaClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {"X-Auth-Token": token, "Content-Type": "application/json"}

    async def search_experts(self, query: str, limit: int = 10) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{EXTELLA_BASE}/api/blocks/search",
                    headers=self.headers, json={"query": query, "limit": limit})
                r.raise_for_status()
                return r.json().get("matches", [])
        except Exception as e:
            logger.error(f"search_experts: {e}")
            return []

    async def run_expert(self, expert_name: str, params: dict | None = None,
                         wait: bool = True, timeout: int = 60,
                         target: str | None = None) -> dict:
        payload: dict = {"expert_name": expert_name, "params": params or {}, "wait": wait}
        if target:
            payload["target"] = target
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{EXTELLA_BASE}/api/expert/run",
                    headers=self.headers, json=payload)
                r.raise_for_status()
                resp = r.json()
            # task_id without result = expert went to local device
            if resp.get("task_id") and not resp.get("result"):
                logger.warning(
                    f"Expert {expert_name} got task_id (ran on default local target). "
                    "This means the Extella token has a registered Mac as default target."
                )
                return {"status": "local_dispatched", "task_id": resp.get("task_id"),
                        "expert_name": expert_name}
            return resp
        except httpx.TimeoutException:
            return {"status": "error", "message": f"Expert timed out ({timeout}s)"}
        except Exception as e:
            logger.error(f"run_expert({expert_name}): {e}")
            return {"status": "error", "message": str(e)}

    async def validate_token(self, token: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{EXTELLA_BASE}/api/token/validate", json={"token": token})
                return r.json().get("valid", False)
        except Exception: return False

    async def list_targets(self, token: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{EXTELLA_BASE}/api/targets/list",
                    headers={"X-Auth-Token": token, "Content-Type": "application/json"},
                    json={})
                return r.json().get("results", [])
        except Exception: return []
