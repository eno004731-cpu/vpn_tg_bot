from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from vpn_bot.models import Base


def build_engine(database_path: Path) -> AsyncEngine:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{database_path}", future=True)


def build_session_factory(database_path: Path) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = build_engine(database_path)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
