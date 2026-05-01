from contextlib import asynccontextmanager
from typing import AsyncGenerator
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Boolean, Text, DateTime, JSON, BigInteger, Integer
from .config import settings

engine = create_async_engine(settings.database_url, echo=settings.debug, pool_pre_ping=True, pool_size=10, max_overflow=20)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class BotExpert(Base):
    __tablename__ = "bot_experts"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    expert_name: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
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
