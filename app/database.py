from contextlib import asynccontextmanager
from typing import AsyncGenerator
from datetime import datetime
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Boolean, Text, DateTime, JSON, BigInteger, Integer
from .config import settings

engine = create_async_engine(settings.database_url, echo=settings.debug,
    pool_pre_ping=True, pool_size=10, max_overflow=20)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str] = mapped_column(String(50), default="start")
    pending_bot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_key_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
        onupdate=datetime.utcnow)


class Bot(Base):
    __tablename__ = "bots"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    token_encrypted: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    bot_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bot_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bot_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # User's own API keys (encrypted JSON): {"fal_api_key": "...", "openai_api_key": "..."}
    user_api_keys_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Local machine connection
    user_extella_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
        onupdate=datetime.utcnow)


class BotExpert(Base):
    __tablename__ = "bot_experts"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    expert_name: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    exec_type: Mapped[str] = mapped_column(String(10), default="cloud")
    trigger_type: Mapped[str] = mapped_column(String(20), default="any")
    trigger_value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    params_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in [
            "ALTER TABLE bots ADD COLUMN IF NOT EXISTS user_api_keys_enc TEXT",
            "ALTER TABLE bots ADD COLUMN IF NOT EXISTS user_extella_token_enc TEXT",
            "ALTER TABLE bots ADD COLUMN IF NOT EXISTS user_target_id VARCHAR(100)",
            "ALTER TABLE bot_experts ADD COLUMN IF NOT EXISTS exec_type VARCHAR(10) DEFAULT 'cloud'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_key_name VARCHAR(50)",
        ]:
            try:
                await conn.execute(sql_text(stmt))
            except Exception:
                pass
