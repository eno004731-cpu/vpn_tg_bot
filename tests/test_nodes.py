from datetime import timedelta
from pathlib import Path

import pytest

from vpn_bot.config import AppSettings, PaymentSettings, Settings, TrafficPolicySettings, XUISettings
from vpn_bot.database import build_session_factory, init_db
from vpn_bot.models import Subscription, User
from vpn_bot.services.nodes import NodeRegistry, NodeRegistryError
from vpn_bot.utils import utc_now


def make_node(code: str, *, enabled: bool = True, priority: int = 100, inbound_id: int = 1) -> XUISettings:
    return XUISettings(
        node_code=code,
        name=code.upper(),
        enabled=enabled,
        priority=priority,
        base_url=f"https://{code}.example.com/secret",
        username="admin",
        password="secret",
        inbound_id=inbound_id,
        public_host=f"{code}.vpn.example.com",
        public_port=443,
    )


def make_settings(nodes: tuple[XUISettings, ...]) -> Settings:
    return Settings(
        app=AppSettings(
            bot_token="token",
            admin_ids=(1,),
            database_path=Path("data/bot.sqlite3"),
        ),
        payment=PaymentSettings(
            bank_name="Demo",
            receiver_name="Demo",
            card_number="0000000000000000",
            phone=None,
            invoice_lifetime_hours=12,
        ),
        traffic_policy=TrafficPolicySettings(),
        xui=nodes[0],
        xui_nodes=nodes,
        secrets_file=Path("secrets/runtime.toml"),
        plans_file=Path("config/plans.toml"),
    )


async def make_session(tmp_path):
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    return engine, session_factory


async def add_subscription(session, *, user_id: int, node_code: str, status: str = "active") -> Subscription:
    subscription = Subscription(
        user_id=user_id,
        source_invoice_id=None,
        plan_code="starter",
        plan_title="Starter",
        status=status,
        node_code=node_code,
        xui_client_id=f"client-{node_code}-{user_id}",
        xui_email=f"tg{user_id}-{node_code}@vpn.local",
        access_url="vless://example",
        traffic_limit_bytes=1024,
        started_at=utc_now(),
        ends_at=utc_now() + timedelta(days=30),
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def test_node_selection_uses_least_active_node(tmp_path) -> None:
    engine, session_factory = await make_session(tmp_path)
    nodes = NodeRegistry([make_node("main"), make_node("nl-2")])
    async with session_factory() as session:
        user = User(tg_id=1, username="user", full_name="User")
        session.add(user)
        await session.flush()
        await add_subscription(session, user_id=user.id, node_code="main")
        await session.commit()

        selected = await nodes.select_node_for_new_subscription(session)

    await nodes.close()
    await engine.dispose()
    assert selected.node_code == "nl-2"


async def test_node_selection_uses_priority_on_tie(tmp_path) -> None:
    engine, session_factory = await make_session(tmp_path)
    nodes = NodeRegistry([make_node("main", priority=100), make_node("nl-2", priority=200)])
    async with session_factory() as session:
        selected = await nodes.select_node_for_new_subscription(session)

    await nodes.close()
    await engine.dispose()
    assert selected.node_code == "nl-2"


async def test_node_selection_skips_disabled_nodes(tmp_path) -> None:
    engine, session_factory = await make_session(tmp_path)
    nodes = NodeRegistry([make_node("main", priority=100), make_node("nl-2", enabled=False, priority=200)])
    async with session_factory() as session:
        selected = await nodes.select_node_for_new_subscription(session)

    await nodes.close()
    await engine.dispose()
    assert selected.node_code == "main"


async def test_node_selection_errors_when_all_nodes_disabled(tmp_path) -> None:
    engine, session_factory = await make_session(tmp_path)
    nodes = NodeRegistry([make_node("main", enabled=False)])
    async with session_factory() as session:
        with pytest.raises(NodeRegistryError, match="Нет включённых"):
            await nodes.select_node_for_new_subscription(session)

    await nodes.close()
    await engine.dispose()


async def test_collect_statuses_continues_when_one_node_fails(tmp_path) -> None:
    engine, session_factory = await make_session(tmp_path)
    nodes = NodeRegistry([make_node("bad"), make_node("good")])

    class GoodClient:
        async def list_inbounds(self):
            return []

    class BadClient:
        async def list_inbounds(self):
            raise RuntimeError("boom")

    nodes._clients = {"bad": BadClient(), "good": GoodClient()}
    async with session_factory() as session:
        statuses = await nodes.collect_statuses(session)

    await engine.dispose()
    by_code = {status.node.node_code: status for status in statuses}
    assert not by_code["bad"].api_ok
    assert by_code["bad"].error == "boom"
    assert by_code["good"].api_ok


async def test_node_registry_reports_unknown_node() -> None:
    nodes = NodeRegistry([make_node("main")])

    with pytest.raises(NodeRegistryError, match="unknown"):
        nodes.get_client("unknown")

    await nodes.close()


def test_settings_all_xui_nodes_falls_back_to_default_node() -> None:
    node = make_node("main")
    settings = make_settings((node,))
    settings.xui_nodes = ()

    assert settings.all_xui_nodes == (node,)
