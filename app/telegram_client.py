import httpx
from typing import Any

class TelegramClient:
    BASE_URL = "https://api.telegram.org"
    def __init__(self, token: str):
        self.token = token
        self.base = f"{self.BASE_URL}/bot{token}"

    async def get_me(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.get(f"{self.base}/getMe")).json()

    async def set_webhook(self, url: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.post(f"{self.base}/setWebhook", json={"url": url})).json()

    async def delete_webhook(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.post(f"{self.base}/deleteWebhook")).json()

    async def send_message(self, chat_id, text: str, parse_mode: str = "HTML", reply_markup=None, disable_notification: bool = False) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096], "parse_mode": parse_mode}
        if disable_notification: payload["disable_notification"] = True
        if reply_markup: payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=15) as c:
            return (await c.post(f"{self.base}/sendMessage", json=payload)).json()

    async def edit_message_text(self, chat_id, message_id: int, text: str, parse_mode: str = "HTML", reply_markup=None) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096], "parse_mode": parse_mode}
        if reply_markup: payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.post(f"{self.base}/editMessageText", json=payload)).json()

    async def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.post(f"{self.base}/answerCallbackQuery", json={"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert})).json()

    async def send_chat_action(self, chat_id, action: str = "typing") -> dict:
        async with httpx.AsyncClient(timeout=5) as c:
            return (await c.post(f"{self.base}/sendChatAction", json={"chat_id": chat_id, "action": action})).json()
