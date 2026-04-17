from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from vpn_bot.config import AppSettings, PaymentSettings, Settings, TrafficPolicySettings, XUISettings
from vpn_bot.database import build_session_factory, init_db
from vpn_bot.models import Invoice, InvoiceStatus, Subscription, SubscriptionStatus, User
from vpn_bot.services.subscriptions import activate_invoice, revoke_subscription, sync_active_subscriptions
from vpn_bot.services.xui import ProvisionedClient, TrafficSnapshot
from vpn_bot.utils import utc_now


def make_node(code: str, *, inbound_id: int) -> XUISettings:
    return XUISettings(
        node_code=code,
        base_url=f"https://{code}.example.com/secret",
        username="admin",
        password="secret",
        inbound_id=inbound_id,
        public_host=f"{code}.vpn.example.com",
        public_port=443,
    )


def make_settings(default_node: XUISettings) -> Settings:
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
        xui=default_node,
        secrets_file=Path("secrets/runtime.toml"),
        plans_file=Path("config/plans.toml"),
    )


class FakePanel:
    def __init__(self, *, traffic: dict[str, TrafficSnapshot] | None = None, fail_fetch: bool = False) -> None:
        self.add_calls = []
        self.revoke_calls = []
        self.speed_calls = []
        self.traffic = traffic or {}
        self.fail_fetch = fail_fetch

    def generate_client_id(self) -> str:
        return "client-id"

    async def add_client(self, inbound_id: int, **kwargs) -> ProvisionedClient:
        self.add_calls.append((inbound_id, kwargs))
        return ProvisionedClient(
            client_id=kwargs["client_id"],
            email=kwargs["email"],
            access_url=f"vless://{kwargs['client_id']}@node",
        )

    async def set_client_enabled(self, inbound_id: int, *, client_id: str, enabled: bool) -> None:
        self.revoke_calls.append((inbound_id, client_id, enabled))

    async def fetch_traffic_map(self) -> dict[str, TrafficSnapshot]:
        if self.fail_fetch:
            raise RuntimeError("node down")
        return self.traffic

    async def update_client_speed_limit(
        self,
        inbound_id: int,
        *,
        client_id: str,
        speed_limit_kbytes_per_second: int,
    ) -> None:
        self.speed_calls.append((inbound_id, client_id, speed_limit_kbytes_per_second))


class FakeNodes:
    def __init__(self, selected_node: XUISettings, panels: dict[str, FakePanel]) -> None:
        self.selected_node = selected_node
        self.panels = panels
        self.nodes = {selected_node.node_code: selected_node}

    async def select_node_for_new_subscription(self, session):
        return self.selected_node

    def get_settings(self, node_code: str) -> XUISettings:
        if node_code in self.nodes:
            return self.nodes[node_code]
        node = make_node(node_code, inbound_id=99)
        self.nodes[node_code] = node
        return node

    def get_client(self, node_code: str) -> FakePanel:
        return self.panels[node_code]


async def make_session(tmp_path):
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    return engine, session_factory


async def add_user(session, tg_id: int = 123) -> User:
    user = User(tg_id=tg_id, username="user", full_name="User")
    session.add(user)
    await session.flush()
    return user


async def add_invoice(session, user: User) -> Invoice:
    invoice = Invoice(
        user_id=user.id,
        plan_code="starter",
        plan_title="Starter",
        duration_days=30,
        traffic_limit_bytes=1024,
        amount_rub=Decimal("100.00"),
        amount_kopecks=10000,
        reference_code="VPN-000001",
        status=InvoiceStatus.pending_review.value,
        expires_at=utc_now() + timedelta(hours=12),
    )
    session.add(invoice)
    await session.flush()
    return invoice


async def add_subscription(session, user: User, *, node_code: str, email: str) -> Subscription:
    subscription = Subscription(
        user_id=user.id,
        source_invoice_id=None,
        plan_code="starter",
        plan_title="Starter",
        status=SubscriptionStatus.active.value,
        node_code=node_code,
        xui_client_id=f"client-{node_code}",
        xui_email=email,
        access_url="vless://example",
        traffic_limit_bytes=1024 * 1024 * 1024,
        started_at=utc_now(),
        ends_at=utc_now() + timedelta(days=30),
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def test_activate_invoice_stores_selected_node_code(tmp_path) -> None:
    node = make_node("nl-2", inbound_id=77)
    panel = FakePanel()
    nodes = FakeNodes(node, {"nl-2": panel})
    settings = make_settings(node)
    engine, session_factory = await make_session(tmp_path)

    async with session_factory() as session:
        user = await add_user(session)
        invoice = await add_invoice(session, user)
        await session.commit()

        result = await activate_invoice(session, settings, nodes, invoice.id)

    await engine.dispose()
    assert result.subscription.node_code == "nl-2"
    assert panel.add_calls[0][0] == 77


async def test_revoke_subscription_uses_subscription_node(tmp_path) -> None:
    default_node = make_node("main", inbound_id=1)
    node = make_node("nl-2", inbound_id=77)
    panel = FakePanel()
    nodes = FakeNodes(default_node, {"nl-2": panel})
    nodes.nodes["nl-2"] = node
    settings = make_settings(default_node)
    engine, session_factory = await make_session(tmp_path)

    async with session_factory() as session:
        user = await add_user(session)
        subscription = await add_subscription(session, user, node_code="nl-2", email="tg123@vpn.local")
        await session.commit()

        revoked = await revoke_subscription(session, settings, nodes, subscription.id)

    await engine.dispose()
    assert revoked.status == SubscriptionStatus.revoked.value
    assert panel.revoke_calls == [(77, "client-nl-2", False)]


async def test_sync_active_subscriptions_continues_when_one_node_fails(tmp_path) -> None:
    main = make_node("main", inbound_id=1)
    good = make_node("good", inbound_id=2)
    good_panel = FakePanel(
        traffic={
            "good@vpn.local": TrafficSnapshot(
                email="good@vpn.local",
                upload_bytes=100,
                download_bytes=200,
                total_bytes=300,
            )
        }
    )
    bad_panel = FakePanel(fail_fetch=True)
    nodes = FakeNodes(main, {"bad": bad_panel, "good": good_panel})
    nodes.nodes.update({"bad": make_node("bad", inbound_id=3), "good": good})
    settings = make_settings(main)
    engine, session_factory = await make_session(tmp_path)

    async with session_factory() as session:
        user = await add_user(session)
        await add_subscription(session, user, node_code="bad", email="bad@vpn.local")
        good_subscription = await add_subscription(session, user, node_code="good", email="good@vpn.local")
        await session.commit()

        await sync_active_subscriptions(session, nodes, settings)
        await session.refresh(good_subscription)

    await engine.dispose()
    assert good_subscription.traffic_used_bytes == 300
    assert good_subscription.upload_bytes == 100
    assert good_subscription.download_bytes == 200
