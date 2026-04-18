from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from vpn_bot.config import PaymentSettings, PlanDefinition
from vpn_bot.database import build_session_factory, init_db
from vpn_bot.models import Invoice, InvoiceStatus, User
from vpn_bot.services.payments import (
    build_stars_payload,
    build_stars_reference,
    create_invoice,
    expire_stale_invoices,
    parse_stars_payload,
    reserve_unique_amount,
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


async def test_expire_stale_invoices_closes_waiting_and_review_invoices(tmp_path) -> None:
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

    assert expired_count == 2
    assert statuses == {
        "VPN-000001": InvoiceStatus.expired.value,
        "VPN-000002": InvoiceStatus.expired.value,
        "VPN-000003": InvoiceStatus.awaiting_transfer.value,
    }
