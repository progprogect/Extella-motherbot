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

    async def get_file_url(self, file_id: str) -> str | None:
        """Resolve Telegram file_id → direct download URL."""
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{self.base}/getFile", params={"file_id": file_id})
            data = r.json()
            if data.get("ok"):
                fp = data["result"]["file_path"]
                return f"https://api.telegram.org/file/bot{self.token}/{fp}"
        return None

    async def set_webhook(self, url: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.post(f"{self.base}/setWebhook", json={"url": url})).json()

    async def delete_webhook(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.post(f"{self.base}/deleteWebhook")).json()

    async def send_message(self, chat_id, text: str, parse_mode: str = "HTML",
                           reply_markup=None, disable_notification: bool = False) -> dict:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text[:4096], "parse_mode": parse_mode}
        if disable_notification:
            payload["disable_notification"] = True
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=15) as c:
            return (await c.post(f"{self.base}/sendMessage", json=payload)).json()

    async def send_photo(self, chat_id, photo: str, caption: str = "",
                         parse_mode: str = "HTML") -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
        if caption:
            payload["caption"] = caption[:1024]
            payload["parse_mode"] = parse_mode
        async with httpx.AsyncClient(timeout=30) as c:
            return (await c.post(f"{self.base}/sendPhoto", json=payload)).json()

    async def send_voice(self, chat_id, voice: str, caption: str = "") -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "voice": voice}
        if caption:
            payload["caption"] = caption[:1024]
        async with httpx.AsyncClient(timeout=30) as c:
            return (await c.post(f"{self.base}/sendVoice", json=payload)).json()

    async def send_audio(self, chat_id, audio: str, caption: str = "",
                         title: str = "") -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "audio": audio}
        if caption:
            payload["caption"] = caption[:1024]
        if title:
            payload["title"] = title
        async with httpx.AsyncClient(timeout=30) as c:
            return (await c.post(f"{self.base}/sendAudio", json=payload)).json()

    async def send_video(self, chat_id, video: str, caption: str = "") -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "video": video}
        if caption:
            payload["caption"] = caption[:1024]
        async with httpx.AsyncClient(timeout=30) as c:
            return (await c.post(f"{self.base}/sendVideo", json=payload)).json()

    async def send_document(self, chat_id, document: str, caption: str = "") -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "document": document}
        if caption:
            payload["caption"] = caption[:1024]
        async with httpx.AsyncClient(timeout=30) as c:
            return (await c.post(f"{self.base}/sendDocument", json=payload)).json()

    async def edit_message_text(self, chat_id, message_id: int, text: str,
                                 parse_mode: str = "HTML", reply_markup=None) -> dict:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id,
            "text": text[:4096], "parse_mode": parse_mode}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.post(f"{self.base}/editMessageText", json=payload)).json()

    async def answer_callback_query(self, callback_query_id: str, text: str = "",
                                     show_alert: bool = False) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            return (await c.post(f"{self.base}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id,
                      "text": text, "show_alert": show_alert})).json()

    async def send_chat_action(self, chat_id, action: str = "typing") -> dict:
        async with httpx.AsyncClient(timeout=5) as c:
            return (await c.post(f"{self.base}/sendChatAction",
                json={"chat_id": chat_id, "action": action})).json()
