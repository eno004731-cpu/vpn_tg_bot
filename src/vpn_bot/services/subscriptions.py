from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vpn_bot.config import Settings
from vpn_bot.models import Invoice, InvoiceStatus, Subscription, SubscriptionStatus, User
from vpn_bot.services.xui import XUIClient
from vpn_bot.utils import ensure_utc, utc_now


@dataclass(frozen=True)
class ActivationResult:
    invoice: Invoice
    subscription: Subscription
    user: User


def build_xui_email(tg_id: int, invoice_id: int, plan_code: str) -> str:
    return f"tg{tg_id}-{plan_code}-{invoice_id}@vpn.local".lower()


async def activate_invoice(
    session: AsyncSession, settings: Settings, panel: XUIClient, invoice_id: int
) -> ActivationResult:
    invoice = await session.scalar(
        select(Invoice)
        .options(selectinload(Invoice.user))
        .where(Invoice.id == invoice_id)
    )
    if invoice is None:
        raise ValueError("Инвойс не найден.")
    if invoice.status == InvoiceStatus.paid.value:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.source_invoice_id == invoice.id)
        )
        if subscription is None:
            raise ValueError("Инвойс уже оплачен, но подписка не найдена.")
        return ActivationResult(invoice=invoice, subscription=subscription, user=invoice.user)
    if invoice.status not in {
        InvoiceStatus.awaiting_transfer.value,
        InvoiceStatus.pending_review.value,
    }:
        raise ValueError("Инвойс нельзя активировать в текущем статусе.")

    now = utc_now()
    client_id = panel.generate_client_id()
    xui_email = build_xui_email(invoice.user.tg_id, invoice.id, invoice.plan_code)
    expires_at = now + timedelta(days=invoice.duration_days)
    provisioned = await panel.add_client(
        settings.xui.inbound_id,
        client_id=client_id,
        email=xui_email,
        traffic_limit_bytes=invoice.traffic_limit_bytes,
        expires_at=expires_at,
        flow=settings.xui.flow,
        telegram_user_id=invoice.user.tg_id,
        comment=invoice.plan_title,
    )
    subscription = Subscription(
        user_id=invoice.user_id,
        source_invoice_id=invoice.id,
        plan_code=invoice.plan_code,
        plan_title=invoice.plan_title,
        status=SubscriptionStatus.active.value,
        xui_client_id=provisioned.client_id,
        xui_email=provisioned.email,
        access_url=provisioned.access_url,
        traffic_limit_bytes=invoice.traffic_limit_bytes,
        started_at=now,
        ends_at=expires_at,
    )
    session.add(subscription)
    invoice.status = InvoiceStatus.paid.value
    invoice.paid_at = now
    await session.commit()
    await session.refresh(subscription)
    await session.refresh(invoice)
    return ActivationResult(invoice=invoice, subscription=subscription, user=invoice.user)


async def sync_active_subscriptions(session: AsyncSession, panel: XUIClient) -> list[Subscription]:
    subscriptions = list(
        await session.scalars(
            select(Subscription)
            .where(Subscription.status == SubscriptionStatus.active.value)
            .options(selectinload(Subscription.user))
        )
    )
    if not subscriptions:
        return []

    traffic_map = await panel.fetch_traffic_map()
    now = utc_now()
    changed = False

    for subscription in subscriptions:
        snapshot = traffic_map.get(subscription.xui_email)
        if snapshot is not None:
            subscription.upload_bytes = snapshot.upload_bytes
            subscription.download_bytes = snapshot.download_bytes
            subscription.traffic_used_bytes = snapshot.total_bytes
            subscription.last_synced_at = now
            changed = True
        if ensure_utc(subscription.ends_at) <= now or subscription.traffic_used_bytes >= subscription.traffic_limit_bytes:
            if subscription.status != SubscriptionStatus.expired.value:
                subscription.status = SubscriptionStatus.expired.value
                changed = True

    if changed:
        await session.commit()
    return subscriptions


async def get_user_active_subscriptions(session: AsyncSession, user_id: int) -> list[Subscription]:
    return list(
        await session.scalars(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status == SubscriptionStatus.active.value,
            )
            .order_by(Subscription.ends_at.desc())
        )
    )


async def get_open_invoices_for_user(session: AsyncSession, user_id: int) -> list[Invoice]:
    return list(
        await session.scalars(
            select(Invoice)
            .where(
                Invoice.user_id == user_id,
                Invoice.status.in_(
                    [
                        InvoiceStatus.awaiting_transfer.value,
                        InvoiceStatus.pending_review.value,
                    ]
                ),
            )
            .order_by(Invoice.created_at.desc())
        )
    )


def format_subscription_lines(subscriptions: Iterable[Subscription]) -> str:
    blocks: list[str] = []
    for item in subscriptions:
        blocks.append(
            "\n".join(
                [
                    f"<b>{item.plan_title}</b>",
                    f"Трафик: <code>{item.traffic_used_bytes}</code> / <code>{item.traffic_limit_bytes}</code> байт",
                    f"До: {ensure_utc(item.ends_at).astimezone().strftime('%Y-%m-%d %H:%M')}",
                    f"Ссылка: <code>{item.access_url}</code>",
                ]
            )
        )
    return "\n\n".join(blocks)
