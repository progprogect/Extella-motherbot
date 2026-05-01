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
                r = await c.post(f"{EXTELLA_BASE}/api/blocks/search", headers=self.headers, json={"query": query, "limit": limit})
                r.raise_for_status()
                return r.json().get("matches", [])
        except Exception as e:
            logger.error(f"search_experts: {e}")
            return []

    async def run_expert(self, expert_name: str, params: dict | None = None, wait: bool = True, timeout: int = 60) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{EXTELLA_BASE}/api/expert/run", headers=self.headers, json={"expert_name": expert_name, "params": params or {}, "wait": wait})
                r.raise_for_status()
                return r.json()
        except httpx.TimeoutException:
            return {"status": "error", "message": f"Expert timed out after {timeout}s"}
        except Exception as e:
            logger.error(f"run_expert({expert_name}): {e}")
            return {"status": "error", "message": str(e)}
