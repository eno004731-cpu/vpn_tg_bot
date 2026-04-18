from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from vpn_bot.config import load_plans, load_settings
from vpn_bot.database import build_session_factory_from_settings, init_db
from vpn_bot.handlers import admin_router, user_router
from vpn_bot.metrics import observe_traffic_sync_failure, render_metrics
from vpn_bot.runtime import AppContext
from vpn_bot.services.jobs import process_one_job, refresh_job_metrics
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
    stop_event = asyncio.Event()
    _install_stop_signal_handlers(stop_event)
    bot = Bot(
        context.settings.app.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    sync_task = asyncio.create_task(background_sync(context, stop_event))
    jobs_task = asyncio.create_task(background_jobs(context, bot, stop_event))
    metrics_runner = await start_worker_metrics_server(context, stop_event)
    try:
        await stop_event.wait()
    finally:
        stop_event.set()
        await asyncio.gather(sync_task, jobs_task, return_exceptions=True)
        if metrics_runner is not None:
            with contextlib.suppress(Exception):
                await metrics_runner.cleanup()
        await context.nodes.close()
        await bot.session.close()
        await context.engine.dispose()


async def background_sync(context: AppContext, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            async with context.session_factory() as session:
                await sync_active_subscriptions(session, context.nodes, context.settings, context.plans)
        except Exception:  # noqa: BLE001
            logging.exception("Traffic sync failed")
            observe_traffic_sync_failure()
        await _sleep_until_stop(stop_event, context.settings.app.sync_interval_seconds)


async def background_jobs(context: AppContext, bot: Bot, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            processed = 0
            while processed < 20 and not stop_event.is_set():
                async with context.session_factory() as session:
                    did_work = await process_one_job(
                        session,
                        context.settings,
                        context.nodes,
                        bot,
                        context.plans,
                    )
                    await refresh_job_metrics(session)
                if not did_work:
                    break
                processed += 1
        except Exception:  # noqa: BLE001
            logging.exception("Job worker failed")
        await _sleep_until_stop(stop_event, context.settings.app.worker_interval_seconds)


async def start_worker_metrics_server(context: AppContext, stop_event: asyncio.Event) -> web.AppRunner | None:
    app = web.Application()
    app.router.add_get("/healthz", worker_healthz)
    app.router.add_get("/metrics", worker_metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=context.settings.app.worker_metrics_host,
        port=int(context.settings.app.worker_metrics_port),
    )
    try:
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    return runner


async def worker_healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def worker_metrics(request: web.Request) -> web.Response:
    payload, content_type = render_metrics()
    return web.Response(body=payload, headers={"Content-Type": content_type})


def _install_stop_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signame, None)
        if signum is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop_event.set)


async def _sleep_until_stop(stop_event: asyncio.Event, seconds: int) -> None:
    if seconds <= 0:
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
