from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
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
        if engine.url.get_backend_name() == "sqlite":
            await _ensure_sqlite_schema(conn)


async def _ensure_sqlite_schema(conn) -> None:
    existing_columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(subscriptions)"))).fetchall()}
    missing_columns = {
        "daily_traffic_date": "ALTER TABLE subscriptions ADD COLUMN daily_traffic_date VARCHAR(10)",
        "daily_baseline_bytes": ("ALTER TABLE subscriptions ADD COLUMN daily_baseline_bytes BIGINT NOT NULL DEFAULT 0"),
        "speed_limit_kbytes_per_second": (
            "ALTER TABLE subscriptions ADD COLUMN speed_limit_kbytes_per_second INTEGER NOT NULL DEFAULT 0"
        ),
        "node_code": "ALTER TABLE subscriptions ADD COLUMN node_code VARCHAR(64) NOT NULL DEFAULT 'main'",
    }
    for column, statement in missing_columns.items():
        if column not in existing_columns:
            await conn.execute(text(statement))
