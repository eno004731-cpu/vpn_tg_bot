from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from time import perf_counter

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from aiohttp import web
from sqlalchemy import text

from vpn_bot.app import create_app_context
from vpn_bot.config import ConfigError
from vpn_bot.handlers import admin_router, user_router
from vpn_bot.metrics import (
    observe_readiness_failure,
    observe_webhook_rejection,
    observe_webhook_request,
    render_metrics,
)
from vpn_bot.runtime import AppContext

TELEGRAM_SECRET_TOKEN_HEADER = "X-Telegram-Bot-Api-Secret-Token"
APP_CONTEXT_KEY = web.AppKey("app_context", AppContext)
BOT_KEY = web.AppKey("bot", Bot)
DISPATCHER_KEY = web.AppKey("dispatcher", Dispatcher)
STOP_EVENT_KEY = web.AppKey("stop_event", asyncio.Event)


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(admin_router)
    dispatcher.include_router(user_router)
    return dispatcher


def create_web_app(
    context: AppContext,
    bot: Bot,
    dispatcher: Dispatcher,
    stop_event: asyncio.Event | None = None,
) -> web.Application:
    app = web.Application()
    app[APP_CONTEXT_KEY] = context
    app[BOT_KEY] = bot
    app[DISPATCHER_KEY] = dispatcher
    app[STOP_EVENT_KEY] = stop_event or asyncio.Event()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", metrics)
    app.router.add_post("/telegram/{secret}", telegram_webhook)
    return app


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def readyz(request: web.Request) -> web.Response:
    if request.app[STOP_EVENT_KEY].is_set():
        return web.json_response({"ok": False, "reason": "draining"}, status=503)
    context = request.app[APP_CONTEXT_KEY]
    try:
        async with context.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        logging.exception("Readiness check failed")
        observe_readiness_failure()
        return web.json_response({"ok": False, "error": str(exc)}, status=503)
    return web.json_response({"ok": True})


async def metrics(request: web.Request) -> web.Response:
    payload, content_type = render_metrics()
    return web.Response(body=payload, headers={"Content-Type": content_type})


async def telegram_webhook(request: web.Request) -> web.Response:
    started_at = perf_counter()
    result = "success"
    context = request.app[APP_CONTEXT_KEY]
    bot = request.app[BOT_KEY]
    dispatcher = request.app[DISPATCHER_KEY]

    try:
        if request.app[STOP_EVENT_KEY].is_set():
            result = "rejected"
            observe_webhook_rejection("draining")
            raise web.HTTPServiceUnavailable(text="shutting down")

        path_secret = context.settings.app.webhook_path_secret
        if not path_secret or request.match_info["secret"] != path_secret:
            result = "rejected"
            observe_webhook_rejection("path_secret")
            raise web.HTTPNotFound()

        expected_secret_token = context.settings.app.webhook_secret_token
        if expected_secret_token and request.headers.get(TELEGRAM_SECRET_TOKEN_HEADER) != expected_secret_token:
            result = "rejected"
            observe_webhook_rejection("secret_token")
            raise web.HTTPUnauthorized(text="wrong telegram secret token")

        try:
            update_data = await request.json()
            update = Update.model_validate(update_data, context={"bot": bot})
        except Exception as exc:  # noqa: BLE001
            result = "rejected"
            observe_webhook_rejection("payload")
            raise web.HTTPBadRequest(text="invalid telegram update payload") from exc

        await dispatcher.feed_update(bot, update, app_context=context)
        return web.json_response({"ok": True})
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001
        result = "error"
        raise
    finally:
        observe_webhook_request(result, perf_counter() - started_at)


async def run_web() -> None:
    context = await create_app_context()
    if not context.settings.app.webhook_path_secret:
        await context.nodes.close()
        await context.engine.dispose()
        raise ConfigError("Для vpn-bot web нужно заполнить webhook_path_secret.")

    stop_event = asyncio.Event()
    bot = Bot(
        context.settings.app.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = build_dispatcher()
    app = create_web_app(context, bot, dispatcher, stop_event)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=context.settings.app.web_host,
        port=int(context.settings.app.web_port),
    )
    try:
        loop = asyncio.get_running_loop()
        for signame in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, signame, None)
            if signum is None:
                continue
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop_event.set)
        await site.start()
        await stop_event.wait()
    finally:
        stop_event.set()
        with contextlib.suppress(Exception):
            await runner.cleanup()
        await context.nodes.close()
        await bot.session.close()
        await context.engine.dispose()
