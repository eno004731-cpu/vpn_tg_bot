from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from vpn_bot.config import AppSettings, PaymentSettings, Settings, TrafficPolicySettings, XUISettings
from vpn_bot.database import build_session_factory, init_db
from vpn_bot.models import Invoice, InvoiceStatus, Job, JobStatus, JobType, Subscription, User
from vpn_bot.services.jobs import process_one_job, schedule_invoice_provisioning
from vpn_bot.services.xui import ProvisionedClient
from vpn_bot.utils import utc_now


def make_settings(node: XUISettings) -> Settings:
    return Settings(
        app=AppSettings(
            bot_token="token",
            admin_ids=(1,),
            database_path=Path("data/bot.sqlite3"),
            field_encryption_key="test-field-key",
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
        secrets_file=Path("secrets/runtime.toml"),
        plans_file=Path("config/plans.toml"),
    )


def make_node() -> XUISettings:
    return XUISettings(
        node_code="main",
        base_url="https://panel.example.com/secret",
        username="admin",
        password="secret",
        inbound_id=7,
        public_host="vpn.example.com",
        public_port=443,
    )


class FakePanel:
    def __init__(self) -> None:
        self.add_calls = []

    def generate_client_id(self) -> str:
        return "client-id"

    async def add_client(self, inbound_id: int, **kwargs) -> ProvisionedClient:
        self.add_calls.append((inbound_id, kwargs))
        return ProvisionedClient(
            client_id=kwargs["client_id"],
            email=kwargs["email"],
            access_url=f"vless://{kwargs['client_id']}@vpn.example.com",
        )


class FakeNodes:
    def __init__(self, node: XUISettings, panel: FakePanel) -> None:
        self.node = node
        self.panel = panel

    async def select_node_for_new_subscription(self, session):
        return self.node

    def get_settings(self, node_code: str) -> XUISettings:
        assert node_code == self.node.node_code
        return self.node

    def get_client(self, node_code: str) -> FakePanel:
        assert node_code == self.node.node_code
        return self.panel


class FakeBot:
    def __init__(self) -> None:
        self.messages = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


async def make_invoice(session) -> Invoice:
    user = User(tg_id=123, username="user", full_name="User")
    session.add(user)
    await session.flush()
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


async def test_schedule_invoice_provisioning_creates_pending_job(tmp_path) -> None:
    node = make_node()
    settings = make_settings(node)
    nodes = FakeNodes(node, FakePanel())
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)

    async with session_factory() as session:
        invoice = await make_invoice(session)
        await session.commit()

        job = await schedule_invoice_provisioning(session, settings, nodes, invoice.id)
        await session.refresh(invoice)

    await engine.dispose()
    assert invoice.status == InvoiceStatus.paid_pending_provision.value
    assert job.type == JobType.provision_access.value
    assert job.status == JobStatus.pending.value


async def test_worker_provisions_access_then_sends_notification(tmp_path) -> None:
    node = make_node()
    settings = make_settings(node)
    panel = FakePanel()
    nodes = FakeNodes(node, panel)
    bot = FakeBot()
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)

    async with session_factory() as session:
        invoice = await make_invoice(session)
        await session.commit()
        await schedule_invoice_provisioning(session, settings, nodes, invoice.id)

    async with session_factory() as session:
        assert await process_one_job(session, settings, nodes, bot)

    async with session_factory() as session:
        subscription = await session.scalar(select(Subscription))
        invoice = await session.get(Invoice, 1)
        send_job = await session.scalar(select(Job).where(Job.type == JobType.send_access_message.value))

    async with session_factory() as session:
        assert await process_one_job(session, settings, nodes, bot)

    await engine.dispose()
    assert invoice is not None
    assert invoice.status == InvoiceStatus.paid.value
    assert subscription is not None
    assert subscription.access_url.startswith("enc:v1:")
    assert send_job is not None
    assert bot.messages and bot.messages[0][0] == 123
