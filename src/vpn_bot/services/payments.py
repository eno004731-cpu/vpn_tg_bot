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
from vpn_bot.services.custom_plans import resolve_plan
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
    """Small read model for showing invoice data without exposing ORM objects."""

    id: int
    amount_rub: Decimal
    reference_code: str
    plan_title: str
    expires_at: str


@dataclass(frozen=True)
class StarsPayload:
    """Structured Telegram Stars payload stored in the Telegram invoice."""

    plan_code: str
    user_tg_id: int


class OneTimePlanAlreadyPurchased(ValueError):
    """Raised when a user tries to buy a one-time tariff again."""

    pass


class OneTimePlanPaymentAlreadyPending(ValueError):
    """Raised when a one-time Stars checkout reservation is still active."""

    pass


def reserve_unique_amount(base_amount: Decimal, used_kopecks: set[int], seed: int) -> Decimal:
    """Pick a transfer amount with unique kopecks among currently open invoices."""

    for offset in range(89):
        suffix = 11 + ((seed + offset) % 89)
        candidate = (base_amount + Decimal(suffix) / Decimal(100)).quantize(Decimal("0.01"))
        if decimal_to_kopecks(candidate) not in used_kopecks:
            return candidate
    raise RuntimeError("Закончились уникальные суммы для открытых инвойсов.")


async def create_invoice(
    session: AsyncSession, user: User, plan: PlanDefinition, payment_settings: PaymentSettings
) -> Invoice:
    """Create a manual-transfer invoice and reserve a unique amount/comment pair."""

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


def build_stars_payload(plan_code: str, user_tg_id: int) -> str:
    """Build compact payload that Telegram returns after Stars payment."""

    return f"stars:{plan_code}:{user_tg_id}"


def parse_stars_payload(payload: str) -> StarsPayload:
    """Parse and validate a Telegram Stars invoice payload."""

    prefix, plan_code, user_tg_id_raw = payload.split(":", maxsplit=2)
    if prefix != "stars" or not plan_code or not user_tg_id_raw.isdigit():
        raise ValueError("Некорректный payload Stars.")
    return StarsPayload(plan_code=plan_code, user_tg_id=int(user_tg_id_raw))


def build_stars_reference(telegram_payment_charge_id: str) -> str:
    """Derive a stable internal payment reference from Telegram's charge id."""

    digest = hashlib.sha256(telegram_payment_charge_id.encode()).hexdigest()[:24]
    return f"XTR-{digest}"


async def user_has_paid_plan(session: AsyncSession, user_id: int, plan_code: str) -> bool:
    """Check whether the user already has a paid invoice for a plan."""

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
    """Create the durable one-time purchase marker before access provisioning."""

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
) -> None:
    """Reserve a one-time Stars checkout before Telegram takes the payment."""

    if not plan.one_time_per_user:
        return

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

    try:
        async with session.begin_nested():
            session.add(
                OneTimePlanReservation(
                    user_id=user_id,
                    plan_code=plan.code,
                    expires_at=now + timedelta(minutes=15),
                )
            )
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


async def release_one_time_stars_checkout(
    session: AsyncSession,
    *,
    user_id: int,
    plan_code: str,
) -> None:
    """Release a temporary one-time Stars checkout reservation."""

    reservation = await session.scalar(
        select(OneTimePlanReservation).where(
            OneTimePlanReservation.user_id == user_id,
            OneTimePlanReservation.plan_code == plan_code,
        )
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
    """Persist an idempotent internal invoice for a successful Stars payment."""

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
    """Render manual payment instructions shown to the buyer."""

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
    """Render the admin review message for a manual transfer invoice."""

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
    """Move an open manual-transfer invoice to admin review."""

    if invoice.status == InvoiceStatus.awaiting_transfer.value:
        invoice.status = InvoiceStatus.pending_review.value


def reject_invoice(invoice: Invoice, note: Optional[str] = None) -> None:
    """Reject an invoice only while it is still safely rejectable."""

    if invoice.status not in REJECTABLE_INVOICE_STATUSES:
        raise ValueError("Инвойс нельзя отклонить в текущем статусе.")
    invoice.status = InvoiceStatus.rejected.value
    invoice.admin_note = note


def expire_open_invoice(invoice: Invoice) -> None:
    """Expire an invoice that is still waiting for transfer confirmation."""

    if invoice.status in EXPIRABLE_INVOICE_STATUSES:
        invoice.status = InvoiceStatus.expired.value


async def expire_stale_invoices(session: AsyncSession) -> int:
    """Auto-expire unpaid invoices whose payment window has ended."""

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
    """Delete temporary Stars reservations after their checkout window expires."""

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
    """Resolve a plan for one-time rules, including dynamic custom plan codes."""

    if plans is None:
        return None
    return resolve_plan(plans, plan_code)


def _one_time_error_message() -> str:
    """Return the user-facing message for already purchased one-time plans."""

    return "Этот тариф можно купить только один раз."


def _one_time_pending_error_message() -> str:
    """Return the user-facing message for an active one-time checkout reservation."""

    return "Оплата по этому тарифу уже открыта. Завершите текущую оплату или подождите 15 минут."
