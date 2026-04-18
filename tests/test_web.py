from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from vpn_bot.config import AppSettings, PaymentSettings, Settings, TrafficPolicySettings, XUISettings
from vpn_bot.database import build_session_factory, init_db
from vpn_bot.metrics import WEBHOOK_REJECTIONS_TOTAL
from vpn_bot.runtime import AppContext
from vpn_bot.services.nodes import NodeRegistry
from vpn_bot.web import configure_telegram_webhook, create_web_app, healthz, metrics, readyz, telegram_webhook


def make_settings() -> Settings:
    node = XUISettings(
        node_code="main",
        base_url="https://panel.example.com/secret",
        username="admin",
        password="secret",
        inbound_id=1,
        public_host="vpn.example.com",
        public_port=443,
    )
    return Settings(
        app=AppSettings(
            bot_token="token",
            admin_ids=(1,),
            database_path=Path("data/bot.sqlite3"),
            webhook_path_secret="telegram-path",
            webhook_secret_token="telegram-token",
        ),
        payment=PaymentSettings(
            bank_name="Demo",
            receiver_name="Demo",
            card_number="0000000000000000",
            phone=None,
            invoice_lifetime_hours=12,
        ),
        traffic_policy=TrafficPolicySettings(),
        xui=node,
        xui_nodes=(node,),
        secrets_file=Path("secrets/runtime.toml"),
        plans_file=Path("config/plans.toml"),
    )


class FakeWebhookBot:
    def __init__(self) -> None:
        self.webhook_calls = []

    async def set_webhook(self, **kwargs) -> None:
        self.webhook_calls.append(kwargs)


async def test_health_ready_and_webhook_secret_check(tmp_path) -> None:
    settings = make_settings()
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(
        settings=settings,
        plans={},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    app = create_web_app(context, bot=object(), dispatcher=object())
    try:
        health = await healthz(make_mocked_request("GET", "/healthz", app=app))
        ready = await readyz(make_mocked_request("GET", "/readyz", app=app))

        with pytest.raises(web.HTTPUnauthorized):
            await telegram_webhook(
                SimpleNamespace(
                    app=app,
                    match_info={"secret": "telegram-path"},
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-token"},
                )
            )
    finally:
        await nodes.close()
        await engine.dispose()

    assert health.status == 200
    assert ready.status == 200


async def test_metrics_endpoint_and_rejection_counter(tmp_path) -> None:
    settings = make_settings()
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(
        settings=settings,
        plans={},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    app = create_web_app(context, bot=object(), dispatcher=object())
    before = WEBHOOK_REJECTIONS_TOTAL.labels(reason="secret_token")._value.get()
    try:
        with pytest.raises(web.HTTPUnauthorized):
            await telegram_webhook(
                SimpleNamespace(
                    app=app,
                    match_info={"secret": "telegram-path"},
                    headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-token"},
                )
            )
        metrics_response = await metrics(make_mocked_request("GET", "/metrics", app=app))
    finally:
        await nodes.close()
        await engine.dispose()

    payload = metrics_response.body.decode("utf-8")
    assert WEBHOOK_REJECTIONS_TOTAL.labels(reason="secret_token")._value.get() == before + 1
    assert "vpn_bot_webhook_requests_total" in payload
    assert "vpn_bot_webhook_rejections_total" in payload


async def test_webhook_rejects_wrong_path_secret(tmp_path) -> None:
    settings = make_settings()
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(
        settings=settings,
        plans={},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    app = create_web_app(context, bot=object(), dispatcher=object())
    try:
        with pytest.raises(web.HTTPNotFound):
            await telegram_webhook(
                SimpleNamespace(
                    app=app,
                    match_info={"secret": "wrong-path"},
                    headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-token"},
                )
            )
    finally:
        await nodes.close()
        await engine.dispose()


async def test_readyz_returns_draining_when_stop_event_set(tmp_path) -> None:
    settings = make_settings()
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    stop_event = asyncio.Event()
    context = AppContext(
        settings=settings,
        plans={},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    app = create_web_app(context, bot=object(), dispatcher=object(), stop_event=stop_event)
    stop_event.set()
    try:
        response = await readyz(make_mocked_request("GET", "/readyz", app=app))
    finally:
        await nodes.close()
        await engine.dispose()

    assert response.status == 503


async def test_configure_telegram_webhook_uses_public_base_url(tmp_path) -> None:
    settings = make_settings()
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(
        settings=settings,
        plans={},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    bot = FakeWebhookBot()
    try:
        await configure_telegram_webhook(bot, context)  # type: ignore[arg-type]
    finally:
        await nodes.close()
        await engine.dispose()

    assert bot.webhook_calls == [
        {
            "url": "https://panel.swift-log.ru/telegram/telegram-path",
            "secret_token": "telegram-token",
            "allowed_updates": ["message", "callback_query", "pre_checkout_query"],
            "drop_pending_updates": False,
        }
    ]
