from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from aiogram.exceptions import TelegramAPIError
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from vpn_bot.config import AppSettings, PaymentSettings, PlanDefinition, Settings, TrafficPolicySettings, XUISettings
from vpn_bot.database import build_session_factory, init_db
from vpn_bot.handlers.user import (
    buy_handler,
    custom_plan_selected,
    invoice_paid,
    stars_payment_selected,
    stars_pre_checkout,
    transfer_payment_selected,
)
from vpn_bot.keyboards import CustomPlanAction, InvoiceAction, PaymentMethodChoice
from vpn_bot.models import Invoice, InvoiceStatus, User
from vpn_bot.runtime import AppContext
from vpn_bot.services.custom_plans import CUSTOM_PLAN_KIND, PREMIUM_PLAN_KIND, build_custom_plan_code
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
    def __init__(self, *, fail_chat_ids: tuple[int, ...] = (), fail_invoice_send: bool = False) -> None:
        self.fail_chat_ids = set(fail_chat_ids)
        self.fail_invoice_send = fail_invoice_send
        self.messages = []
        self.invoices = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:
        if chat_id in self.fail_chat_ids:
            raise TelegramAPIError(SimpleNamespace(__api_method__="sendMessage"), "boom")
        self.messages.append((chat_id, text, reply_markup))

    async def send_invoice(self, **kwargs) -> None:
        if self.fail_invoice_send:
            raise TelegramAPIError(SimpleNamespace(__api_method__="sendInvoice"), "boom")
        self.invoices.append(kwargs)


class FakeMessage:
    def __init__(self, user_id: int = 123) -> None:
        self.from_user = TelegramUser(id=user_id, is_bot=False, first_name="User", username="user")
        self.answers = []
        self.edits = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))

    async def edit_text(self, text: str, reply_markup=None) -> None:
        self.edits.append((text, reply_markup))


class FakeCallback:
    def __init__(self, *, user_id: int, bot: FakeBot) -> None:
        self.from_user = TelegramUser(id=user_id, is_bot=False, first_name="User", username="user")
        self.bot = bot
        self.message = FakeMessage(user_id=user_id)
        self.answers = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class FakePreCheckout:
    def __init__(self, *, user_id: int, payload: str, total_amount: int) -> None:
        self.invoice_payload = payload
        self.currency = "XTR"
        self.total_amount = total_amount
        self.from_user = TelegramUser(id=user_id, is_bot=False, first_name="User", username="user")
        self.answers = []

    async def answer(self, ok: bool, error_message: str | None = None) -> None:
        self.answers.append((ok, error_message))


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


def make_trial_plan() -> PlanDefinition:
    return PlanDefinition(
        code="stars_test",
        title="3 дня / 30 ГБ",
        price_rub=Decimal("0.00"),
        price_stars=1,
        duration_days=3,
        traffic_limit_gb=30,
        device_limit=1,
        one_time_per_user=True,
    )


def make_one_time_transfer_plan() -> PlanDefinition:
    return PlanDefinition(
        code="transfer_trial",
        title="Transfer trial",
        price_rub=Decimal("100.00"),
        duration_days=3,
        traffic_limit_gb=30,
        device_limit=1,
        one_time_per_user=True,
    )


async def test_buy_handler_shows_custom_builders(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    plan = PlanDefinition(
        code="starter",
        title="Starter",
        price_rub=Decimal("100.00"),
        duration_days=30,
        traffic_limit_gb=310,
    )
    context = AppContext(
        settings=settings,
        plans={plan.code: plan},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    message = FakeMessage(user_id=123)

    try:
        await buy_handler(message, context)
    finally:
        await nodes.close()
        await engine.dispose()

    assert message.answers
    markup = message.answers[0][1]
    button_texts = [button.text for row in markup.inline_keyboard for button in row]
    assert button_texts[:2] == ["Собрать Custom", "Собрать Custom Premium"]
    assert "Starter - 100.00 ₽" in button_texts


async def test_custom_plan_builder_updates_days_and_devices(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    callback = FakeCallback(user_id=123, bot=FakeBot())

    try:
        await custom_plan_selected(
            callback,
            CustomPlanAction(kind=CUSTOM_PLAN_KIND, days=30, devices=1, action="up1"),
            context,
        )
    finally:
        await nodes.close()
        await engine.dispose()

    assert callback.answers == [("", False)]
    assert callback.message.answers == []
    assert callback.message.edits
    text, markup = callback.message.edits[0]
    assert "Custom: 30 дней / 2 устройств / 756 ГБ" in text
    assert "Стоимость: <code>240.00</code> ₽ / <code>240</code> ⭐" in text
    assert markup is not None


async def test_custom_plan_builder_ignores_noop_day_increase_at_max(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    callback = FakeCallback(user_id=123, bot=FakeBot())

    try:
        await custom_plan_selected(
            callback,
            CustomPlanAction(kind=CUSTOM_PLAN_KIND, days=365, devices=1, action="dp1"),
            context,
        )
    finally:
        await nodes.close()
        await engine.dispose()

    assert callback.answers == [("Уже максимум: 365 дней.", False)]
    assert callback.message.edits == []
    assert callback.message.answers == []


async def test_custom_plan_builder_ignores_noop_day_decrease_at_min(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    callback = FakeCallback(user_id=123, bot=FakeBot())

    try:
        await custom_plan_selected(
            callback,
            CustomPlanAction(kind=CUSTOM_PLAN_KIND, days=1, devices=1, action="dm1"),
            context,
        )
    finally:
        await nodes.close()
        await engine.dispose()

    assert callback.answers == [("Уже минимум: 1 день.", False)]
    assert callback.message.edits == []
    assert callback.message.answers == []


async def test_custom_plan_builder_ignores_noop_device_increase_at_max(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    callback = FakeCallback(user_id=123, bot=FakeBot())

    try:
        await custom_plan_selected(
            callback,
            CustomPlanAction(kind=PREMIUM_PLAN_KIND, days=30, devices=10, action="up1"),
            context,
        )
    finally:
        await nodes.close()
        await engine.dispose()

    assert callback.answers == [("Уже максимум: 10 устройств.", False)]
    assert callback.message.edits == []
    assert callback.message.answers == []


async def test_custom_premium_builder_pay_opens_payment_methods(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    callback = FakeCallback(user_id=123, bot=FakeBot())

    try:
        await custom_plan_selected(
            callback,
            CustomPlanAction(kind=PREMIUM_PLAN_KIND, days=30, devices=2, action="pay"),
            context,
        )
    finally:
        await nodes.close()
        await engine.dispose()

    assert callback.answers == [("", False)]
    text, markup = callback.message.answers[0]
    assert "Custom Premium: 30 дней / 2 устройств / Безлимит" in text
    assert markup is not None
    button_texts = [button.text for row in markup.inline_keyboard for button in row]
    assert button_texts == ["Перевод на карту / СБП", "Оплатить Stars: 540 ⭐"]


async def test_dynamic_custom_transfer_invoice_uses_calculated_plan(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    callback = FakeCallback(user_id=123, bot=FakeBot())
    plan_code = build_custom_plan_code(CUSTOM_PLAN_KIND, 30, 2)

    try:
        await transfer_payment_selected(callback, PaymentMethodChoice(code=plan_code, method="transfer"), context)
        async with session_factory() as session:
            invoice = await session.scalar(select(Invoice).where(Invoice.plan_code == plan_code))
    finally:
        await nodes.close()
        await engine.dispose()

    assert invoice is not None
    assert invoice.plan_title == "Custom: 30 дней / 2 устройств / 756 ГБ"
    assert invoice.duration_days == 30
    assert invoice.traffic_limit_bytes == 756 * 1024 * 1024 * 1024
    assert invoice.amount_rub >= Decimal("240.00")
    assert callback.answers == [("Инвойс создан", False)]


async def test_dynamic_premium_stars_payment_uses_calculated_price(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    bot = FakeBot()
    callback = FakeCallback(user_id=123, bot=bot)
    plan_code = build_custom_plan_code(PREMIUM_PLAN_KIND, 30, 2)

    try:
        await stars_payment_selected(callback, PaymentMethodChoice(code=plan_code, method="stars"), context)
    finally:
        await nodes.close()
        await engine.dispose()

    assert callback.answers == [("Открыл оплату Stars", False)]
    assert len(bot.invoices) == 1
    invoice = bot.invoices[0]
    assert invoice["title"] == "Custom Premium: 30 дней / 2 устройств / Безлимит"
    assert invoice["payload"] == f"stars:{plan_code}:123"
    assert invoice["prices"][0].amount == 540


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


async def test_invoice_paid_rolls_back_when_all_admin_notifications_fail(tmp_path) -> None:
    settings = make_settings(admin_ids=(1, 2))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    context = AppContext(settings=settings, plans={}, engine=engine, session_factory=session_factory, nodes=nodes)
    bot = FakeBot(fail_chat_ids=(1, 2))
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
    assert stored_invoice.status == InvoiceStatus.awaiting_transfer.value
    assert bot.messages == []
    assert callback.answers == [("Не смог уведомить администратора. Попробуйте ещё раз.", True)]
    assert callback.message.answers == [("Не смог отправить платёж администратору. Нажмите «Я оплатил» ещё раз.", None)]


async def test_stars_payment_selected_blocks_second_trial_purchase(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    plan = make_trial_plan()
    context = AppContext(
        settings=settings,
        plans={plan.code: plan},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    bot = FakeBot()
    callback = FakeCallback(user_id=123, bot=bot)

    try:
        async with session_factory() as session:
            invoice = await add_invoice(session, tg_id=123, status=InvoiceStatus.paid.value)
            invoice.plan_code = plan.code
            invoice.paid_at = utc_now()
            await session.commit()

        await stars_payment_selected(callback, PaymentMethodChoice(code=plan.code, method="stars"), context)
    finally:
        await nodes.close()
        await engine.dispose()

    assert bot.invoices == []
    assert callback.answers == [("Этот тариф можно купить только один раз.", True)]


async def test_stars_payment_selected_blocks_second_parallel_open_payment(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    plan = make_trial_plan()
    context = AppContext(
        settings=settings,
        plans={plan.code: plan},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    bot = FakeBot()
    first_callback = FakeCallback(user_id=123, bot=bot)
    second_callback = FakeCallback(user_id=123, bot=bot)

    try:
        await stars_payment_selected(first_callback, PaymentMethodChoice(code=plan.code, method="stars"), context)
        await stars_payment_selected(second_callback, PaymentMethodChoice(code=plan.code, method="stars"), context)
    finally:
        await nodes.close()
        await engine.dispose()

    assert len(bot.invoices) == 1
    assert first_callback.answers == [("Открыл оплату Stars", False)]
    assert second_callback.answers == [
        ("Оплата по этому тарифу уже открыта. Завершите текущую оплату или подождите 15 минут.", True)
    ]


async def test_stars_payment_selected_releases_reservation_when_send_invoice_fails(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    plan = make_trial_plan()
    failing_bot = FakeBot(fail_invoice_send=True)
    retry_bot = FakeBot()
    failing_callback = FakeCallback(user_id=123, bot=failing_bot)
    retry_callback = FakeCallback(user_id=123, bot=retry_bot)
    context = AppContext(
        settings=settings,
        plans={plan.code: plan},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )

    try:
        await stars_payment_selected(failing_callback, PaymentMethodChoice(code=plan.code, method="stars"), context)
        await stars_payment_selected(retry_callback, PaymentMethodChoice(code=plan.code, method="stars"), context)
    finally:
        await nodes.close()
        await engine.dispose()

    assert failing_callback.answers == [("Не удалось открыть оплату Stars. Попробуйте ещё раз.", True)]
    assert retry_callback.answers == [("Открыл оплату Stars", False)]
    assert len(retry_bot.invoices) == 1


async def test_transfer_payment_selected_blocks_second_one_time_purchase(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    plan = make_one_time_transfer_plan()
    context = AppContext(
        settings=settings,
        plans={plan.code: plan},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    bot = FakeBot()
    callback = FakeCallback(user_id=123, bot=bot)

    try:
        async with session_factory() as session:
            invoice = await add_invoice(session, tg_id=123, status=InvoiceStatus.paid.value)
            invoice.plan_code = plan.code
            invoice.paid_at = utc_now()
            await session.commit()

        await transfer_payment_selected(callback, PaymentMethodChoice(code=plan.code, method="transfer"), context)

        async with session_factory() as session:
            invoice_count = len(list(await session.scalars(select(Invoice))))
    finally:
        await nodes.close()
        await engine.dispose()

    assert invoice_count == 1
    assert callback.answers == [("Этот тариф можно купить только один раз.", True)]
    assert callback.message.answers == []


async def test_stars_pre_checkout_rejects_second_trial_purchase(tmp_path) -> None:
    settings = make_settings(admin_ids=(1,))
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    nodes = NodeRegistry.from_settings(settings)
    plan = make_trial_plan()
    context = AppContext(
        settings=settings,
        plans={plan.code: plan},
        engine=engine,
        session_factory=session_factory,
        nodes=nodes,
    )
    pre_checkout = FakePreCheckout(user_id=123, payload="stars:stars_test:123", total_amount=1)

    try:
        async with session_factory() as session:
            invoice = await add_invoice(session, tg_id=123, status=InvoiceStatus.paid.value)
            invoice.plan_code = plan.code
            invoice.paid_at = utc_now()
            await session.commit()

        await stars_pre_checkout(pre_checkout, context)
    finally:
        await nodes.close()
        await engine.dispose()

    assert pre_checkout.answers == [(False, "Этот тариф можно купить только один раз.")]
