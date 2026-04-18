from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from vpn_bot.config import AppSettings
from vpn_bot.models import Base


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + database_url.removeprefix("postgresql://")
    if database_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + database_url.removeprefix("postgres://")
    return database_url


def build_engine(database_path: Optional[Path] = None, database_url: Optional[str] = None) -> AsyncEngine:
    if database_url:
        return create_async_engine(normalize_database_url(database_url), future=True)
    if database_path is None:
        raise ValueError("database_path is required when database_url is not set")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{database_path}", future=True)


def build_session_factory(
    database_path: Optional[Path] = None,
    database_url: Optional[str] = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = build_engine(database_path, database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def build_session_factory_from_settings(settings: AppSettings) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    return build_session_factory(settings.database_path, settings.database_url)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        backend = engine.url.get_backend_name()
        if backend == "sqlite":
            await _ensure_sqlite_schema(conn)
        elif backend == "postgresql":
            await _ensure_postgres_schema(conn)


async def _ensure_sqlite_schema(conn) -> None:
    existing_columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(subscriptions)"))).fetchall()}
    missing_columns = {
        "daily_traffic_date": "ALTER TABLE subscriptions ADD COLUMN daily_traffic_date VARCHAR(10)",
        "daily_baseline_bytes": ("ALTER TABLE subscriptions ADD COLUMN daily_baseline_bytes BIGINT NOT NULL DEFAULT 0"),
        "speed_limit_kbytes_per_second": (
            "ALTER TABLE subscriptions ADD COLUMN speed_limit_kbytes_per_second INTEGER NOT NULL DEFAULT 0"
        ),
        "node_code": "ALTER TABLE subscriptions ADD COLUMN node_code VARCHAR(64) NOT NULL DEFAULT 'main'",
        "access_sent_at": "ALTER TABLE subscriptions ADD COLUMN access_sent_at DATETIME",
    }
    for column, statement in missing_columns.items():
        if column not in existing_columns:
            await conn.execute(text(statement))


async def _ensure_postgres_schema(conn) -> None:
    result = await conn.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'subscriptions'
            """
        )
    )
    existing_columns = {row[0] for row in result.fetchall()}
    missing_columns = {
        "daily_traffic_date": "ALTER TABLE subscriptions ADD COLUMN daily_traffic_date VARCHAR(10)",
        "daily_baseline_bytes": ("ALTER TABLE subscriptions ADD COLUMN daily_baseline_bytes BIGINT NOT NULL DEFAULT 0"),
        "speed_limit_kbytes_per_second": (
            "ALTER TABLE subscriptions ADD COLUMN speed_limit_kbytes_per_second INTEGER NOT NULL DEFAULT 0"
        ),
        "node_code": "ALTER TABLE subscriptions ADD COLUMN node_code VARCHAR(64) NOT NULL DEFAULT 'main'",
        "access_sent_at": "ALTER TABLE subscriptions ADD COLUMN access_sent_at TIMESTAMP WITH TIME ZONE",
    }
    for column, statement in missing_columns.items():
        if column not in existing_columns:
            await conn.execute(text(statement))

    if "xui_client_id" in existing_columns:
        await conn.execute(text("ALTER TABLE subscriptions ALTER COLUMN xui_client_id TYPE TEXT"))
    if "xui_email" in existing_columns:
        await conn.execute(text("ALTER TABLE subscriptions ALTER COLUMN xui_email TYPE TEXT"))
