from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from vpn_bot.app import run_bot, run_worker
from vpn_bot.config import load_settings
from vpn_bot.database import build_session_factory_from_settings, init_db
from vpn_bot.migrations import migrate_sqlite_to_postgres
from vpn_bot.web import run_web


async def init_db_command() -> None:
    """CLI command that creates or updates database tables."""

    settings = load_settings()
    engine, _ = build_session_factory_from_settings(settings.app)
    try:
        await init_db(engine)
    finally:
        await engine.dispose()


async def migrate_sqlite_to_postgres_command(sqlite_path: str, database_url: str) -> None:
    """CLI command that copies all bot data from SQLite into PostgreSQL."""

    summary = await migrate_sqlite_to_postgres(Path(sqlite_path), database_url)
    print(  # noqa: T201
        "Migrated "
        f"users={summary.users}, "
        f"invoices={summary.invoices}, "
        f"one_time_plan_purchases={summary.one_time_plan_purchases}, "
        f"one_time_plan_reservations={summary.one_time_plan_reservations}, "
        f"subscriptions={summary.subscriptions}, "
        f"jobs={summary.jobs}"
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the vpn_bot command-line parser and subcommands."""

    parser = argparse.ArgumentParser(description="VPN subscription bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Run Telegram bot with polling")
    subparsers.add_parser("web", help="Run aiohttp Telegram webhook server")
    subparsers.add_parser("worker", help="Run provisioning and traffic worker")
    subparsers.add_parser("init-db", help="Create database tables")
    migrate_parser = subparsers.add_parser("migrate-sqlite-to-postgres", help="Copy SQLite data into Postgres")
    migrate_parser.add_argument("--sqlite", required=True, help="Path to SQLite database")
    migrate_parser.add_argument("--database-url", required=True, help="Postgres SQLAlchemy URL")
    return parser


def main() -> None:
    """Dispatch the selected CLI subcommand."""

    parser = build_parser()
    args = parser.parse_args()
    if args.command == "init-db":
        asyncio.run(init_db_command())
        return
    if args.command == "run":
        asyncio.run(run_bot())
        return
    if args.command == "web":
        asyncio.run(run_web())
        return
    if args.command == "worker":
        asyncio.run(run_worker())
        return
    if args.command == "migrate-sqlite-to-postgres":
        asyncio.run(migrate_sqlite_to_postgres_command(args.sqlite, args.database_url))
        return
    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
