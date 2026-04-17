from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from aiogram.exceptions import TelegramAPIError
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from vpn_bot.config import AppSettings, PaymentSettings, Settings, TrafficPolicySettings, XUISettings
from vpn_bot.database import build_session_factory, init_db
from vpn_bot.handlers.user import invoice_paid
from vpn_bot.keyboards import InvoiceAction
from vpn_bot.models import Invoice, InvoiceStatus, User
from vpn_bot.runtime import AppContext
from vpn_bot.services.nodes import NodeRegistry
from vpn_bot.utils import utc_now


def make_settings(*, admin_ids: tuple[int, ...]) -> Settings:
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
            admin_ids=admin_ids,
            database_path=Path("data/bot.sqlite3"),
        ),
        payment=PaymentSettings(
            bank_name="Demo Bank",
            receiver_name="Demo User",
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


class FakeBot:
    def __init__(self, *, fail_chat_ids: tuple[int, ...] = ()) -> None:
        self.fail_chat_ids = set(fail_chat_ids)
        self.messages = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        if chat_id in self.fail_chat_ids:
            raise TelegramAPIError(SimpleNamespace(__api_method__="sendMessage"), "boom")
        self.messages.append((chat_id, text, reply_markup))


class FakeMessage:
    def __init__(self) -> None:
        self.answers = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))


class FakeCallback:
    def __init__(self, *, user_id: int, bot: FakeBot) -> None:
        self.from_user = TelegramUser(id=user_id, is_bot=False, first_name="User", username="user")
        self.bot = bot
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


async def add_invoice(session, *, tg_id: int, status: str) -> Invoice:
    user = User(tg_id=tg_id, username="user", full_name="User")
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
        status=status,
        expires_at=utc_now() + timedelta(hours=12),
    )
    session.add(invoice)
    await session.flush()
    return invoice


async def test_invoice_paid_does_not_re_notify_paid_invoice(tmp_path) -> None:
    settings = make_settings(admin_ids=(1, 2))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    bot = FakeBot()
    callback = FakeCallback(user_id=123, bot=bot)

    try:
        async with session_factory() as session:
            invoice = await add_invoice(session, tg_id=123, status=InvoiceStatus.paid.value)
            await session.commit()

        await invoice_paid(callback, InvoiceAction(action="paid", invoice_id=invoice.id), context)
    finally:
        await nodes.close()
        await engine.dispose()

    assert bot.messages == []
    assert callback.answers == [("Оплата по этому инвойсу уже подтверждена.", False)]
    assert callback.message.answers == []


async def test_invoice_paid_continues_when_one_admin_notification_fails(tmp_path) -> None:
    settings = make_settings(admin_ids=(1, 2))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    bot = FakeBot(fail_chat_ids=(1,))
    callback = FakeCallback(user_id=123, bot=bot)

    try:
        async with session_factory() as session:
            invoice = await add_invoice(session, tg_id=123, status=InvoiceStatus.awaiting_transfer.value)
            await session.commit()

        await invoice_paid(callback, InvoiceAction(action="paid", invoice_id=invoice.id), context)

        async with session_factory() as session:
            stored_invoice = await session.scalar(select(Invoice).where(Invoice.id == invoice.id))
    finally:
        await nodes.close()
        await engine.dispose()

    assert stored_invoice is not None
    assert stored_invoice.status == InvoiceStatus.pending_review.value
    assert [chat_id for chat_id, _, _ in bot.messages] == [2]
    assert callback.answers == [("Передал платёж админу на проверку.", False)]
    assert callback.message.answers == [
        ("Платёж отправлен на проверку. Как только подтвержу перевод, пришлю доступ.", None)
    ]
