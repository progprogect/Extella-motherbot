import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, BackgroundTasks
from .database import init_db
from .telegram_client import TelegramClient
from .motherbot import handle_motherbot_update
from .bot_router import handle_user_bot_update
from .config import settings

logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
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

app = FastAPI(title="Extella Motherbot", version="1.0.0", lifespan=lifespan,
              docs_url="/docs" if settings.debug else None)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "extella-motherbot", "version": "1.0.0"}

@app.get("/")
async def root():
    return {"service": "Extella Motherbot", "version": "1.0.0"}

@app.post("/motherbot/webhook")
async def motherbot_webhook(request: Request, background_tasks: BackgroundTasks):
    background_tasks.add_task(handle_motherbot_update, await request.json())
    return {"ok": True}

@app.post("/bot/{token_hash}/webhook")
async def user_bot_webhook(token_hash: str, request: Request, background_tasks: BackgroundTasks):
    background_tasks.add_task(handle_user_bot_update, token_hash, await request.json())
    return {"ok": True}
