from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from vpn_bot.config import load_plans, load_settings
from vpn_bot.database import build_session_factory_from_settings, init_db
from vpn_bot.handlers import admin_router, user_router
from vpn_bot.runtime import AppContext
from vpn_bot.services.jobs import process_one_job
from vpn_bot.services.nodes import NodeRegistry
from vpn_bot.services.subscriptions import sync_active_subscriptions


async def create_app_context() -> AppContext:
    settings = load_settings()
    plans = load_plans(settings.plans_file)
    engine, session_factory = build_session_factory_from_settings(settings.app)
    await init_db(engine)

    nodes = NodeRegistry.from_settings(settings)
    return AppContext(
        settings=settings,
        plans=plans,
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )


async def run_bot() -> None:
    context = await create_app_context()
    bot = Bot(
        context.settings.app.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(admin_router)
    dispatcher.include_router(user_router)

    sync_task = asyncio.create_task(background_sync(context))
    jobs_task = asyncio.create_task(background_jobs(context, bot))
    try:
        await dispatcher.start_polling(bot, app_context=context)
    finally:
        sync_task.cancel()
        jobs_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sync_task
        with contextlib.suppress(asyncio.CancelledError):
            await jobs_task
        await context.nodes.close()
        await bot.session.close()
        await context.engine.dispose()


async def run_worker() -> None:
    context = await create_app_context()
    bot = Bot(
        context.settings.app.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    sync_task = asyncio.create_task(background_sync(context))
    jobs_task = asyncio.create_task(background_jobs(context, bot))
    try:
        await asyncio.gather(sync_task, jobs_task)
    finally:
        sync_task.cancel()
        jobs_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sync_task
        with contextlib.suppress(asyncio.CancelledError):
            await jobs_task
        await context.nodes.close()
        await bot.session.close()
        await context.engine.dispose()


async def background_sync(context: AppContext) -> None:
    while True:
        try:
            async with context.session_factory() as session:
                await sync_active_subscriptions(session, context.nodes, context.settings, context.plans)
        except Exception:  # noqa: BLE001
            logging.exception("Traffic sync failed")
        await asyncio.sleep(context.settings.app.sync_interval_seconds)


async def background_jobs(context: AppContext, bot: Bot) -> None:
    while True:
        try:
            processed = 0
            while processed < 20:
                async with context.session_factory() as session:
                    did_work = await process_one_job(session, context.settings, context.nodes, bot)
                if not did_work:
                    break
                processed += 1
        except Exception:  # noqa: BLE001
            logging.exception("Job worker failed")
        await asyncio.sleep(context.settings.app.worker_interval_seconds)
