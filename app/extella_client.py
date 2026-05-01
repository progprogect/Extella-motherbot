import httpx
import logging
import asyncio
logger = logging.getLogger(__name__)
EXTELLA_BASE = 'https://api.extella.ai'

class ExtellaClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {'X-Auth-Token': token, 'Content-Type': 'application/json'}

    async def search_experts(self, query: str, limit: int = 10) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f'{EXTELLA_BASE}/api/blocks/search',
                    headers=self.headers, json={'query': query, 'limit': limit})
                r.raise_for_status()
                return r.json().get('matches', [])
        except Exception as e:
            logger.error(f'search_experts: {e}')
            return []

    async def run_expert(self, expert_name: str, params: dict | None = None,
                         wait: bool = True, timeout: int = 60,
                         target: str | None = None) -> dict:
        payload = {'expert_name': expert_name, 'params': params or {}, 'wait': wait}
        if target:
            payload['target'] = target
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f'{EXTELLA_BASE}/api/expert/run',
                    headers=self.headers, json=payload)
                r.raise_for_status()
                resp = r.json()
            # If got async task_id without result, poll for it
            if resp.get('task_id') and not resp.get('result'):
                resp = await self._poll_task(resp['task_id'], timeout=timeout)
            return resp
        except httpx.TimeoutException:
            return {'status': 'error', 'message': f'Expert timed out ({timeout}s)'}
        except Exception as e:
            logger.error(f'run_expert({expert_name}): {e}')
            return {'status': 'error', 'message': str(e)}

    async def _poll_task(self, task_id: str, timeout: int = 60) -> dict:
        """Poll /api/task/check until target is free (= task done)."""
        poll_url = f'{EXTELLA_BASE}/api/task/check'
        for attempt in range(timeout // 3):
            await asyncio.sleep(3)
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post(poll_url,
                        headers=self.headers, json={'task_id': task_id})
                    data = r.json()
                    status = data.get('status', '')
                    # 'running' = target free = DONE (counter-intuitive Extella convention)
                    if status == 'running':
                        result = data.get('result', data.get('output', {}))
                        if result:
                            return {'status': 'success', 'result': result}
                        # Task done but result embedded differently
                        return {'status': 'success', 'result': data}
                    elif status == 'busy':
                        logger.debug(f'Task {task_id} still running (t={attempt*3}s)')
                        continue
                    elif status == 'error':
                        return {'status': 'error', 'message': data.get('message', 'Task failed')}
            except Exception as e:
                logger.warning(f'Poll error: {e}')
        return {'status': 'error', 'message': f'Task {task_id} timed out after {timeout}s'}

    async def validate_token(self, token: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f'{EXTELLA_BASE}/api/token/validate',
                    json={'token': token})
                return r.json().get('valid', False)
        except Exception: return False

    async def list_targets(self, token: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f'{EXTELLA_BASE}/api/targets/list',
                    headers={'X-Auth-Token': token, 'Content-Type': 'application/json'},
                    json={})
                return r.json().get('results', [])
        except Exception: return []
