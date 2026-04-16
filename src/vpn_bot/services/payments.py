from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from html import escape
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpn_bot.config import PaymentSettings, PlanDefinition
from vpn_bot.models import Invoice, InvoiceStatus, User
from vpn_bot.utils import decimal_to_kopecks, ensure_utc, format_card_number, utc_now


OPEN_INVOICE_STATUSES = (
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
                Invoice.status.in_(OPEN_INVOICE_STATUSES),
                Invoice.id != invoice.id,
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


def format_invoice_for_user(invoice: Invoice, payment_settings: PaymentSettings) -> str:
    payment_lines = []
    if payment_settings.phone:
        payment_lines.append(
            f"СБП по телефону: <code>{escape(payment_settings.phone)}</code>"
        )
        payment_lines.append(
            f"Карта для перевода: <code>{format_card_number(payment_settings.card_number)}</code>"
        )
    else:
        payment_lines.append(
            f"Карта для перевода: <code>{format_card_number(payment_settings.card_number)}</code>"
        )

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
    invoice.status = InvoiceStatus.rejected.value
    invoice.admin_note = note


def expire_open_invoice(invoice: Invoice) -> None:
    if invoice.status in OPEN_INVOICE_STATUSES:
        invoice.status = InvoiceStatus.expired.value
