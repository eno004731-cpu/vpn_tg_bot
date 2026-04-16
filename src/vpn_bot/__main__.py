from __future__ import annotations

import argparse
import asyncio

from vpn_bot.app import run_bot
from vpn_bot.config import load_settings
from vpn_bot.database import build_session_factory, init_db


async def init_db_command() -> None:
    settings = load_settings()
    engine, _ = build_session_factory(settings.app.database_path)
    try:
        await init_db(engine)
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VPN subscription bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Run Telegram bot")
    subparsers.add_parser("init-db", help="Create database tables")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "init-db":
        asyncio.run(init_db_command())
        return
    if args.command == "run":
        asyncio.run(run_bot())
        return
    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
