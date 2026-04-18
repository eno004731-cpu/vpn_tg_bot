from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from html import escape
from typing import Mapping, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_bot.config import PaymentSettings, PlanDefinition
from vpn_bot.models import Invoice, InvoiceStatus, OneTimePlanPurchase, OneTimePlanReservation, User
from vpn_bot.utils import decimal_to_kopecks, ensure_utc, format_card_number, utc_now

EXPIRABLE_INVOICE_STATUSES = (InvoiceStatus.awaiting_transfer.value,)
PURCHASED_PLAN_STATUSES = (
    InvoiceStatus.paid_pending_provision.value,
    InvoiceStatus.provision_failed.value,
    InvoiceStatus.paid.value,
)
REJECTABLE_INVOICE_STATUSES = (
    InvoiceStatus.awaiting_transfer.value,
    InvoiceStatus.pending_review.value,
)


@dataclass(frozen=True)
class InvoiceView:
    id: int
    amount_rub: Decimal
    reference_code: str
    plan_title: str
    expires_at: str


@dataclass(frozen=True)
class StarsPayload:
    plan_code: str
    user_tg_id: int
    reservation_id: Optional[int] = None
    reservation_created_at_us: Optional[int] = None


class OneTimePlanAlreadyPurchased(ValueError):
    pass


class OneTimePlanPaymentAlreadyPending(ValueError):
    pass


def reserve_unique_amount(base_amount: Decimal, used_kopecks: set[int], seed: int) -> Decimal:
    for offset in range(89):
        suffix = 11 + ((seed + offset) % 89)
        candidate = (base_amount + Decimal(suffix) / Decimal(100)).quantize(Decimal("0.01"))
        if decimal_to_kopecks(candidate) not in used_kopecks:
            return candidate
    raise RuntimeError("Закончились уникальные суммы для открытых инвойсов.")


async def create_invoice(
    session: AsyncSession, user: User, plan: PlanDefinition, payment_settings: PaymentSettings
) -> Invoice:
    now = utc_now()
    invoice = Invoice(
        user_id=user.id,
        plan_code=plan.code,
        plan_title=plan.title,
        duration_days=plan.duration_days,
        traffic_limit_bytes=plan.traffic_limit_bytes,
        amount_rub=plan.price_rub,
        amount_kopecks=decimal_to_kopecks(plan.price_rub),
        reference_code="pending",
        status=InvoiceStatus.awaiting_transfer.value,
        expires_at=now + timedelta(hours=payment_settings.invoice_lifetime_hours),
    )
    session.add(invoice)
    await session.flush()

    used_kopecks = set(
        await session.scalars(
            select(Invoice.amount_kopecks).where(
                Invoice.id != invoice.id,
                or_(
                    and_(
                        Invoice.status == InvoiceStatus.awaiting_transfer.value,
                        Invoice.expires_at > now,
                    ),
                    Invoice.status == InvoiceStatus.pending_review.value,
                ),
            )
        )
    )

    amount_rub = reserve_unique_amount(plan.price_rub, used_kopecks, invoice.id)
    invoice.amount_rub = amount_rub
    invoice.amount_kopecks = decimal_to_kopecks(amount_rub)
    invoice.reference_code = f"VPN-{invoice.id:06d}"

    await session.commit()
    await session.refresh(invoice)
    return invoice


def build_stars_payload(
    plan_code: str,
    user_tg_id: int,
    reservation_id: Optional[int] = None,
    reservation_created_at_us: Optional[int] = None,
) -> str:
    if reservation_created_at_us is not None and reservation_id is None:
        raise ValueError("reservation_created_at_us requires reservation_id")
    parts = ["stars", plan_code, str(user_tg_id)]
    if reservation_id is not None:
        parts.append(str(reservation_id))
    if reservation_created_at_us is not None:
        parts.append(str(reservation_created_at_us))
    return ":".join(parts)


def parse_stars_payload(payload: str) -> StarsPayload:
    parts = payload.split(":", maxsplit=4)
    if len(parts) not in {3, 4, 5}:
        raise ValueError("Некорректный payload Stars.")
    prefix, plan_code, user_tg_id_raw, *reservation_parts = parts
    if prefix != "stars" or not plan_code or not user_tg_id_raw.isdigit():
        raise ValueError("Некорректный payload Stars.")
    reservation_id = None
    reservation_created_at_us = None
    if reservation_parts:
        reservation_id_raw = reservation_parts[0]
        if not reservation_id_raw.isdigit():
            raise ValueError("Некорректный payload Stars.")
        reservation_id = int(reservation_id_raw)
    if len(reservation_parts) == 2:
        reservation_created_at_us_raw = reservation_parts[1]
        if not reservation_created_at_us_raw.isdigit():
            raise ValueError("Некорректный payload Stars.")
        reservation_created_at_us = int(reservation_created_at_us_raw)
    return StarsPayload(
        plan_code=plan_code,
        user_tg_id=int(user_tg_id_raw),
        reservation_id=reservation_id,
        reservation_created_at_us=reservation_created_at_us,
    )


def build_stars_reference(telegram_payment_charge_id: str) -> str:
    digest = hashlib.sha256(telegram_payment_charge_id.encode()).hexdigest()[:24]
    return f"XTR-{digest}"


async def user_has_paid_plan(session: AsyncSession, user_id: int, plan_code: str) -> bool:
    invoice_id = await session.scalar(
        select(Invoice.id)
        .where(
            Invoice.user_id == user_id,
            Invoice.plan_code == plan_code,
            Invoice.status.in_(PURCHASED_PLAN_STATUSES),
        )
        .limit(1)
    )
    return invoice_id is not None


async def reserve_one_time_plan_purchase(
    session: AsyncSession,
    invoice: Invoice,
    plans: Optional[Mapping[str, PlanDefinition]],
) -> None:
    plan = _get_plan(plans, invoice.plan_code)
    if plan is None or not plan.one_time_per_user:
        return

    existing_purchase = await session.scalar(
        select(OneTimePlanPurchase).where(
            OneTimePlanPurchase.user_id == invoice.user_id,
            OneTimePlanPurchase.plan_code == invoice.plan_code,
        )
    )
    if existing_purchase is not None:
        if existing_purchase.invoice_id == invoice.id:
            return
        raise OneTimePlanAlreadyPurchased(_one_time_error_message())

    existing_paid_invoice_id = await session.scalar(
        select(Invoice.id)
        .where(
            Invoice.user_id == invoice.user_id,
            Invoice.plan_code == invoice.plan_code,
            Invoice.id != invoice.id,
            Invoice.status.in_(PURCHASED_PLAN_STATUSES),
        )
        .limit(1)
    )
    if existing_paid_invoice_id is not None:
        raise OneTimePlanAlreadyPurchased(_one_time_error_message())

    try:
        async with session.begin_nested():
            session.add(
                OneTimePlanPurchase(
                    user_id=invoice.user_id,
                    plan_code=invoice.plan_code,
                    invoice_id=invoice.id,
                )
            )
            await session.flush()
    except IntegrityError as exc:
        raise OneTimePlanAlreadyPurchased(_one_time_error_message()) from exc


async def reserve_one_time_stars_checkout(
    session: AsyncSession,
    *,
    user_id: int,
    plan: PlanDefinition,
) -> Optional[OneTimePlanReservation]:
    if not plan.one_time_per_user:
        return None

    now = utc_now()
    active_reservation = await session.scalar(
        select(OneTimePlanReservation).where(
            OneTimePlanReservation.user_id == user_id,
            OneTimePlanReservation.plan_code == plan.code,
        )
    )
    if active_reservation is not None:
        if ensure_utc(active_reservation.expires_at) > now:
            raise OneTimePlanPaymentAlreadyPending(_one_time_pending_error_message())
        await session.delete(active_reservation)
        await session.flush()

    if await user_has_paid_plan(session, user_id, plan.code):
        raise OneTimePlanAlreadyPurchased(_one_time_error_message())

    reservation = OneTimePlanReservation(
        user_id=user_id,
        plan_code=plan.code,
        expires_at=now + timedelta(minutes=15),
    )
    try:
        async with session.begin_nested():
            session.add(reservation)
            await session.flush()
    except IntegrityError as exc:
        existing = await session.scalar(
            select(OneTimePlanReservation).where(
                OneTimePlanReservation.user_id == user_id,
                OneTimePlanReservation.plan_code == plan.code,
            )
        )
        if existing is not None and ensure_utc(existing.expires_at) > now:
            raise OneTimePlanPaymentAlreadyPending(_one_time_pending_error_message()) from exc
        raise
    return reservation


async def get_one_time_stars_checkout(
    session: AsyncSession,
    *,
    reservation_id: int,
) -> Optional[OneTimePlanReservation]:
    reservation = await session.scalar(
        select(OneTimePlanReservation).where(OneTimePlanReservation.id == reservation_id)
    )
    if reservation is None:
        return None
    if ensure_utc(reservation.expires_at) > utc_now():
        return reservation
    await session.delete(reservation)
    await session.flush()
    return None


async def release_one_time_stars_checkout(
    session: AsyncSession,
    *,
    reservation_id: int,
) -> None:
    reservation = await session.scalar(
        select(OneTimePlanReservation).where(OneTimePlanReservation.id == reservation_id)
    )
    if reservation is not None:
        await session.delete(reservation)
        await session.flush()


async def create_stars_invoice_record(
    session: AsyncSession,
    user: User,
    plan: PlanDefinition,
    telegram_payment_charge_id: str,
    total_stars: int,
) -> Invoice:
    reference_code = build_stars_reference(telegram_payment_charge_id)
    existing = await session.scalar(select(Invoice).where(Invoice.reference_code == reference_code))
    if existing is not None:
        return existing

    now = utc_now()
    invoice = Invoice(
        user_id=user.id,
        plan_code=plan.code,
        plan_title=plan.title,
        duration_days=plan.duration_days,
        traffic_limit_bytes=plan.traffic_limit_bytes,
        amount_rub=Decimal(total_stars),
        amount_kopecks=total_stars,
        reference_code=reference_code,
        status=InvoiceStatus.awaiting_transfer.value,
        expires_at=now + timedelta(minutes=15),
    )
    try:
        async with session.begin_nested():
            session.add(invoice)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(select(Invoice).where(Invoice.reference_code == reference_code))
        if existing is not None:
            return existing
        raise
    return invoice


def format_invoice_for_user(invoice: Invoice, payment_settings: PaymentSettings) -> str:
    payment_lines = []
    if payment_settings.phone:
        payment_lines.append(f"СБП по телефону: <code>{escape(payment_settings.phone)}</code>")
        payment_lines.append(f"Карта для перевода: <code>{format_card_number(payment_settings.card_number)}</code>")
    else:
        payment_lines.append(f"Карта для перевода: <code>{format_card_number(payment_settings.card_number)}</code>")

    lines = [
        f"<b>{escape(invoice.plan_title)}</b>",
        "",
        f"Сумма перевода: <code>{invoice.amount_rub}</code> ₽",
        f"Банк: {escape(payment_settings.bank_name)}",
        f"Получатель: {escape(payment_settings.receiver_name)}",
    ]
    lines.extend(payment_lines)
    lines.append(f"Комментарий к переводу: <code>{invoice.reference_code}</code>")
    if payment_settings.instruction_hint:
        lines.extend(["", escape(payment_settings.instruction_hint)])
    lines.extend(
        [
            "",
            "После оплаты нажмите кнопку <b>Я оплатил</b>.",
            f"Инвойс действует до: {ensure_utc(invoice.expires_at).astimezone().strftime('%Y-%m-%d %H:%M')}",
        ]
    )
    return "\n".join(lines)


def format_invoice_for_admin(invoice: Invoice, user: User) -> str:
    return "\n".join(
        [
            "<b>Новый платёж на проверку</b>",
            f"Invoice ID: <code>{invoice.id}</code>",
            f"Пользователь: <code>{user.tg_id}</code> @{escape(user.username or '-')}",
            f"Тариф: {escape(invoice.plan_title)}",
            f"Сумма: <code>{invoice.amount_rub}</code> ₽",
            f"Комментарий: <code>{invoice.reference_code}</code>",
        ]
    )


def mark_invoice_pending_review(invoice: Invoice) -> None:
    if invoice.status == InvoiceStatus.awaiting_transfer.value:
        invoice.status = InvoiceStatus.pending_review.value


def reject_invoice(invoice: Invoice, note: Optional[str] = None) -> None:
    if invoice.status not in REJECTABLE_INVOICE_STATUSES:
        raise ValueError("Инвойс нельзя отклонить в текущем статусе.")
    invoice.status = InvoiceStatus.rejected.value
    invoice.admin_note = note


def expire_open_invoice(invoice: Invoice) -> None:
    if invoice.status in EXPIRABLE_INVOICE_STATUSES:
        invoice.status = InvoiceStatus.expired.value


async def expire_stale_invoices(session: AsyncSession) -> int:
    now = utc_now()
    invoices = list(
        await session.scalars(
            select(Invoice).where(
                Invoice.status.in_(EXPIRABLE_INVOICE_STATUSES),
                Invoice.expires_at <= now,
            )
        )
    )
    for invoice in invoices:
        expire_open_invoice(invoice)
        if not invoice.admin_note:
            invoice.admin_note = "Автоматически закрыт по истечении времени ожидания оплаты."
    if invoices:
        await session.commit()
    return len(invoices)


async def purge_stale_one_time_reservations(session: AsyncSession) -> int:
    now = utc_now()
    reservations = list(
        await session.scalars(select(OneTimePlanReservation).where(OneTimePlanReservation.expires_at <= now))
    )
    for reservation in reservations:
        await session.delete(reservation)
    if reservations:
        await session.commit()
    return len(reservations)


def _get_plan(plans: Optional[Mapping[str, PlanDefinition]], plan_code: str) -> Optional[PlanDefinition]:
    if plans is None:
        return None
    get = getattr(plans, "get", None)
    if get is None:
        return None
    return get(plan_code)


def _one_time_error_message() -> str:
    return "Этот тариф можно купить только один раз."


def _one_time_pending_error_message() -> str:
    return "Оплата по этому тарифу уже открыта. Завершите текущую оплату или подождите 15 минут."
