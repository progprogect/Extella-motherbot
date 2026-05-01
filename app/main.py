import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, BackgroundTasks
from sqlalchemy import select
from .database import Bot, init_db, get_session
from .telegram_client import TelegramClient
from .motherbot import handle_motherbot_update
from .bot_router import handle_user_bot_update, _respond, extella
from .crypto import decrypt_token
from .config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database tables ready")
    if settings.motherbot_token and settings.railway_url:
        mb = TelegramClient(settings.motherbot_token)
        res = await mb.set_webhook(f"{settings.railway_url}/motherbot/webhook")
        logger.info(f"Motherbot webhook: {res}")
    else:
        logger.warning("MOTHERBOT_TOKEN or RAILWAY_URL missing")
    yield


app = FastAPI(
    title="Extella Motherbot",
    version="8.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "extella-motherbot", "version": "8.0.0"}


@app.get("/")
async def root():
    return {"service": "Extella Motherbot", "version": "8.0.0"}


@app.post("/motherbot/webhook")
async def motherbot_webhook(request: Request, background_tasks: BackgroundTasks):
    background_tasks.add_task(handle_motherbot_update, await request.json())
    return {"ok": True}


@app.post("/bot/{token_hash}/webhook")
async def user_bot_webhook(
    token_hash: str, request: Request, background_tasks: BackgroundTasks
):
    background_tasks.add_task(handle_user_bot_update, token_hash, await request.json())
    return {"ok": True}


@app.post("/expert_result/{token_hash}/{chat_id}")
async def expert_result_callback(
    token_hash: str,
    chat_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Callback endpoint for local experts running on user's device.
    Experts can POST their result here; Railway delivers it to Telegram.

    Expected body: {"result": {...}, "expert_name": "..."}
    or any JSON that _respond() can handle.
    """
    try:
        data = await request.json()
        background_tasks.add_task(
            _deliver_callback_result, token_hash, chat_id, data
        )
        return {"ok": True, "message": "Result received, sending to Telegram"}
    except Exception as e:
        logger.error(f"Callback parse error: {e}")
        return {"ok": False, "error": str(e)}


async def _deliver_callback_result(token_hash: str, chat_id: int, data: dict):
    """Deliver async expert result from device to Telegram."""
    try:
        async with get_session() as session:
            bot = (await session.execute(
                select(Bot).where(
                    Bot.token_hash == token_hash, Bot.is_active == True
                )
            )).scalar_one_or_none()
            if not bot:
                logger.warning(f"Callback: no bot for hash={token_hash}")
                return
            raw = decrypt_token(bot.token_encrypted, settings.secret_key)
            utg = TelegramClient(raw)
            expert_name = data.get("expert_name", "expert")
            await _respond(utg, chat_id, data, False, expert_name)
    except Exception as e:
        logger.error(f"Callback deliver error: {e}", exc_info=True)
