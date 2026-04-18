from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from vpn_bot.config import PaymentSettings, PlanDefinition
from vpn_bot.database import build_session_factory, init_db
from vpn_bot.models import Invoice, InvoiceStatus, OneTimePlanReservation, User
from vpn_bot.services.payments import (
    OneTimePlanAlreadyPurchased,
    OneTimePlanPaymentAlreadyPending,
    build_stars_payload,
    build_stars_reference,
    create_invoice,
    expire_stale_invoices,
    parse_stars_payload,
    purge_stale_one_time_reservations,
    reject_invoice,
    release_one_time_stars_checkout,
    reserve_one_time_plan_purchase,
    reserve_one_time_stars_checkout,
    reserve_unique_amount,
    user_has_paid_plan,
)
from vpn_bot.services.subscriptions import get_open_invoices_for_user
from vpn_bot.utils import decimal_to_kopecks, utc_now


def test_reserve_unique_amount_skips_existing_values() -> None:
    base = Decimal("299.00")
    used = {decimal_to_kopecks(Decimal("299.11")), decimal_to_kopecks(Decimal("299.12"))}

    candidate = reserve_unique_amount(base, used, seed=0)

    assert candidate == Decimal("299.13")


def test_stars_payload_round_trip() -> None:
    payload = build_stars_payload("starter", 123456789)

    parsed = parse_stars_payload(payload)

    assert parsed.plan_code == "starter"
    assert parsed.user_tg_id == 123456789
    assert parsed.reservation_id is None
    assert parsed.reservation_created_at_us is None


def test_stars_payload_round_trip_with_reservation() -> None:
    payload = build_stars_payload("starter", 123456789, 77, 1_700_000_000)

    parsed = parse_stars_payload(payload)

    assert parsed.plan_code == "starter"
    assert parsed.user_tg_id == 123456789
    assert parsed.reservation_id == 77
    assert parsed.reservation_created_at_us == 1_700_000_000


def test_stars_reference_is_stable_and_short() -> None:
    reference = build_stars_reference("charge-id")

    assert reference == build_stars_reference("charge-id")
    assert reference.startswith("XTR-")
    assert len(reference) <= 64


async def test_create_invoice_reuses_amount_from_expired_awaiting_transfer(tmp_path) -> None:
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)

    async with session_factory() as session:
        user = User(tg_id=123, username="user", full_name="User")
        session.add(user)
        await session.flush()
        session.add(
            Invoice(
                user_id=user.id,
                plan_code="starter",
                plan_title="Starter",
                duration_days=30,
                traffic_limit_bytes=10,
                amount_rub=Decimal("100.13"),
                amount_kopecks=10013,
                reference_code="VPN-000001",
                status=InvoiceStatus.awaiting_transfer.value,
                expires_at=utc_now() - timedelta(minutes=5),
            )
        )
        await session.flush()

        invoice = await create_invoice(
            session,
            user,
            PlanDefinition(
                code="starter",
                title="Starter",
                price_rub=Decimal("100.00"),
                duration_days=30,
                traffic_limit_gb=1,
            ),
            PaymentSettings(
                bank_name="Demo Bank",
                receiver_name="Demo User",
                card_number="0000000000000000",
                phone=None,
                invoice_lifetime_hours=12,
            ),
        )

    await engine.dispose()

    assert invoice.amount_rub == Decimal("100.13")


async def test_get_open_invoices_for_user_hides_expired_awaiting_transfer(tmp_path) -> None:
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)

    async with session_factory() as session:
        user = User(tg_id=123, username="user", full_name="User")
        session.add(user)
        await session.flush()
        session.add_all(
            [
                Invoice(
                    user_id=user.id,
                    plan_code="starter",
                    plan_title="Starter",
                    duration_days=30,
                    traffic_limit_bytes=10,
                    amount_rub=Decimal("100.00"),
                    amount_kopecks=10000,
                    reference_code="VPN-000001",
                    status=InvoiceStatus.awaiting_transfer.value,
                    expires_at=utc_now() - timedelta(minutes=5),
                ),
                Invoice(
                    user_id=user.id,
                    plan_code="starter",
                    plan_title="Starter",
                    duration_days=30,
                    traffic_limit_bytes=10,
                    amount_rub=Decimal("100.01"),
                    amount_kopecks=10001,
                    reference_code="VPN-000002",
                    status=InvoiceStatus.pending_review.value,
                    expires_at=utc_now() - timedelta(minutes=5),
                ),
            ]
        )
        await session.commit()

        invoices = await get_open_invoices_for_user(session, user.id)

    await engine.dispose()

    assert [invoice.reference_code for invoice in invoices] == ["VPN-000002"]


async def test_expire_stale_invoices_closes_only_waiting_invoices(tmp_path) -> None:
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)

    async with session_factory() as session:
        user = User(tg_id=123, username="user", full_name="User")
        session.add(user)
        await session.flush()
        session.add_all(
            [
                Invoice(
                    user_id=user.id,
                    plan_code="starter",
                    plan_title="Starter",
                    duration_days=30,
                    traffic_limit_bytes=10,
                    amount_rub=Decimal("100.00"),
                    amount_kopecks=10000,
                    reference_code="VPN-000001",
                    status=InvoiceStatus.awaiting_transfer.value,
                    expires_at=utc_now() - timedelta(minutes=1),
                ),
                Invoice(
                    user_id=user.id,
                    plan_code="starter",
                    plan_title="Starter",
                    duration_days=30,
                    traffic_limit_bytes=10,
                    amount_rub=Decimal("100.01"),
                    amount_kopecks=10001,
                    reference_code="VPN-000002",
                    status=InvoiceStatus.pending_review.value,
                    expires_at=utc_now() - timedelta(minutes=1),
                ),
                Invoice(
                    user_id=user.id,
                    plan_code="starter",
                    plan_title="Starter",
                    duration_days=30,
                    traffic_limit_bytes=10,
                    amount_rub=Decimal("100.02"),
                    amount_kopecks=10002,
                    reference_code="VPN-000003",
                    status=InvoiceStatus.awaiting_transfer.value,
                    expires_at=utc_now() + timedelta(hours=1),
                ),
            ]
        )
        await session.commit()

        expired_count = await expire_stale_invoices(session)
        result = await session.execute(select(Invoice.reference_code, Invoice.status))
        statuses = {reference: status for reference, status in result}

    await engine.dispose()

    assert expired_count == 1
    assert statuses == {
        "VPN-000001": InvoiceStatus.expired.value,
        "VPN-000002": InvoiceStatus.pending_review.value,
        "VPN-000003": InvoiceStatus.awaiting_transfer.value,
    }


async def test_reject_invoice_refuses_paid_invoice(tmp_path) -> None:
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)

    async with session_factory() as session:
        user = User(tg_id=123, username="user", full_name="User")
        session.add(user)
        await session.flush()
        invoice = Invoice(
            user_id=user.id,
            plan_code="starter",
            plan_title="Starter",
            duration_days=30,
            traffic_limit_bytes=10,
            amount_rub=Decimal("100.00"),
            amount_kopecks=10000,
            reference_code="VPN-000001",
            status=InvoiceStatus.paid.value,
            expires_at=utc_now() - timedelta(minutes=1),
            paid_at=utc_now(),
        )
        session.add(invoice)
        await session.flush()

        try:
            reject_invoice(invoice)
        except ValueError as exc:
            message = str(exc)
        else:
            message = ""

    await engine.dispose()

    assert message == "Инвойс нельзя отклонить в текущем статусе."
    assert invoice.status == InvoiceStatus.paid.value


async def test_one_time_plan_purchase_reservation_blocks_second_invoice(tmp_path) -> None:
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    plan = PlanDefinition(
        code="trial",
        title="Trial",
        price_rub=Decimal("100.00"),
        duration_days=3,
        traffic_limit_gb=30,
        one_time_per_user=True,
    )

    async with session_factory() as session:
        user = User(tg_id=123, username="user", full_name="User")
        session.add(user)
        await session.flush()
        first = Invoice(
            user_id=user.id,
            plan_code=plan.code,
            plan_title=plan.title,
            duration_days=plan.duration_days,
            traffic_limit_bytes=plan.traffic_limit_bytes,
            amount_rub=Decimal("100.00"),
            amount_kopecks=10000,
            reference_code="VPN-000001",
            status=InvoiceStatus.pending_review.value,
            expires_at=utc_now() + timedelta(hours=1),
        )
        second = Invoice(
            user_id=user.id,
            plan_code=plan.code,
            plan_title=plan.title,
            duration_days=plan.duration_days,
            traffic_limit_bytes=plan.traffic_limit_bytes,
            amount_rub=Decimal("100.01"),
            amount_kopecks=10001,
            reference_code="VPN-000002",
            status=InvoiceStatus.pending_review.value,
            expires_at=utc_now() + timedelta(hours=1),
        )
        session.add_all([first, second])
        await session.flush()

        await reserve_one_time_plan_purchase(session, first, {plan.code: plan})
        try:
            await reserve_one_time_plan_purchase(session, second, {plan.code: plan})
        except OneTimePlanAlreadyPurchased as exc:
            message = str(exc)
        else:
            message = ""

    await engine.dispose()

    assert message == "Этот тариф можно купить только один раз."


async def test_user_has_paid_plan_ignores_rejected_paid_duplicate(tmp_path) -> None:
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)

    async with session_factory() as session:
        user = User(tg_id=123, username="user", full_name="User")
        session.add(user)
        await session.flush()
        session.add(
            Invoice(
                user_id=user.id,
                plan_code="trial",
                plan_title="Trial",
                duration_days=3,
                traffic_limit_bytes=10,
                amount_rub=Decimal("1.00"),
                amount_kopecks=100,
                reference_code="XTR-duplicate",
                status=InvoiceStatus.rejected.value,
                expires_at=utc_now() + timedelta(minutes=15),
                paid_at=utc_now(),
            )
        )
        await session.commit()

        result = await user_has_paid_plan(session, user.id, "trial")

    await engine.dispose()

    assert result is False


async def test_one_time_stars_checkout_blocks_second_open_window(tmp_path) -> None:
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    plan = PlanDefinition(
        code="trial",
        title="Trial",
        price_rub=Decimal("0.00"),
        price_stars=1,
        duration_days=3,
        traffic_limit_gb=30,
        one_time_per_user=True,
    )

    async with session_factory() as session:
        user = User(tg_id=123, username="user", full_name="User")
        session.add(user)
        await session.flush()

        await reserve_one_time_stars_checkout(session, user_id=user.id, plan=plan)
        try:
            await reserve_one_time_stars_checkout(session, user_id=user.id, plan=plan)
        except OneTimePlanPaymentAlreadyPending as exc:
            message = str(exc)
        else:
            message = ""

    await engine.dispose()

    assert message == "Оплата по этому тарифу уже открыта. Завершите текущую оплату или подождите 15 минут."


async def test_release_and_purge_stale_one_time_stars_checkout(tmp_path) -> None:
    engine, session_factory = build_session_factory(tmp_path / "bot.sqlite3")
    await init_db(engine)
    plan = PlanDefinition(
        code="trial",
        title="Trial",
        price_rub=Decimal("0.00"),
        price_stars=1,
        duration_days=3,
        traffic_limit_gb=30,
        one_time_per_user=True,
    )

    async with session_factory() as session:
        user = User(tg_id=123, username="user", full_name="User")
        session.add(user)
        await session.flush()
        reservation = await reserve_one_time_stars_checkout(session, user_id=user.id, plan=plan)
        assert reservation is not None
        await release_one_time_stars_checkout(session, reservation_id=reservation.id)
        await session.commit()
        await reserve_one_time_stars_checkout(session, user_id=user.id, plan=plan)
        reservation = await session.scalar(select(OneTimePlanReservation))
        assert reservation is not None

    async with session_factory() as session:
        reservations = list(await session.scalars(select(OneTimePlanReservation)))
        reservations[0].expires_at = utc_now() - timedelta(minutes=1)
        await session.commit()
        purged = await purge_stale_one_time_reservations(session)
        remaining = list(await session.scalars(select(OneTimePlanReservation)))

    await engine.dispose()

    assert purged == 1
    assert remaining == []
