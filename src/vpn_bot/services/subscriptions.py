from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Mapping, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vpn_bot.config import PlanDefinition, Settings, TrafficPolicySettings
from vpn_bot.models import Invoice, InvoiceStatus, Subscription, SubscriptionStatus, User
from vpn_bot.services.xui import TrafficSnapshot, XUIClient
from vpn_bot.utils import ensure_utc, utc_now


@dataclass(frozen=True)
class ActivationResult:
    invoice: Invoice
    subscription: Subscription
    user: User


def build_xui_email(tg_id: int, invoice_id: int, plan_code: str) -> str:
    return f"tg{tg_id}-{plan_code}-{invoice_id}@vpn.local".lower()


def build_manual_xui_email(tg_id: int, plan_code: str) -> str:
    return f"tg{tg_id}-{plan_code}-manual-{uuid4().hex[:8]}@vpn.local".lower()


async def activate_invoice(
    session: AsyncSession, settings: Settings, panel: XUIClient, invoice_id: int
) -> ActivationResult:
    invoice = await session.scalar(select(Invoice).options(selectinload(Invoice.user)).where(Invoice.id == invoice_id))
    if invoice is None:
        raise ValueError("Инвойс не найден.")
    if invoice.status == InvoiceStatus.paid.value:
        subscription = await session.scalar(select(Subscription).where(Subscription.source_invoice_id == invoice.id))
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
        node_code=settings.xui.node_code,
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


async def provision_subscription_for_user(
    session: AsyncSession,
    settings: Settings,
    panel: XUIClient,
    user: User,
    *,
    plan_code: str,
    plan_title: str,
    duration_days: int,
    traffic_limit_bytes: int,
    source_invoice_id: Optional[int] = None,
) -> Subscription:
    now = utc_now()
    client_id = panel.generate_client_id()
    xui_email = build_manual_xui_email(user.tg_id, plan_code)
    expires_at = now + timedelta(days=duration_days)
    provisioned = await panel.add_client(
        settings.xui.inbound_id,
        client_id=client_id,
        email=xui_email,
        traffic_limit_bytes=traffic_limit_bytes,
        expires_at=expires_at,
        flow=settings.xui.flow,
        telegram_user_id=user.tg_id,
        comment=plan_title,
    )
    subscription = Subscription(
        user_id=user.id,
        source_invoice_id=source_invoice_id,
        plan_code=plan_code,
        plan_title=plan_title,
        status=SubscriptionStatus.active.value,
        node_code=settings.xui.node_code,
        xui_client_id=provisioned.client_id,
        xui_email=provisioned.email,
        access_url=provisioned.access_url,
        traffic_limit_bytes=traffic_limit_bytes,
        started_at=now,
        ends_at=expires_at,
    )
    session.add(subscription)
    await session.commit()
    await session.refresh(subscription)
    return subscription


async def revoke_subscription(
    session: AsyncSession,
    settings: Settings,
    panel: XUIClient,
    subscription_id: int,
) -> Subscription:
    subscription = await session.scalar(
        select(Subscription).options(selectinload(Subscription.user)).where(Subscription.id == subscription_id)
    )
    if subscription is None:
        raise ValueError("Подписка не найдена.")
    if subscription.status != SubscriptionStatus.active.value:
        raise ValueError("Подписка уже не активна.")

    await panel.set_client_enabled(
        settings.xui.inbound_id,
        client_id=subscription.xui_client_id,
        enabled=False,
    )
    subscription.status = SubscriptionStatus.revoked.value
    subscription.speed_limit_kbytes_per_second = 0
    await session.commit()
    await session.refresh(subscription)
    return subscription


async def sync_active_subscriptions(
    session: AsyncSession,
    panel: XUIClient,
    settings: Optional[Settings] = None,
    plans: Optional[Mapping[str, PlanDefinition]] = None,
) -> list[Subscription]:
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
            if settings is not None and await _apply_daily_traffic_policy(
                subscription,
                snapshot,
                panel,
                settings,
                plan_daily_limit_bytes=_get_plan_daily_limit_bytes(subscription, plans),
            ):
                changed = True
        if (
            ensure_utc(subscription.ends_at) <= now
            or subscription.traffic_used_bytes >= subscription.traffic_limit_bytes
        ):
            if subscription.status != SubscriptionStatus.expired.value:
                subscription.status = SubscriptionStatus.expired.value
                changed = True

    if changed:
        await session.commit()
    return subscriptions


async def _apply_daily_traffic_policy(
    subscription: Subscription,
    snapshot: TrafficSnapshot,
    panel: XUIClient,
    settings: Settings,
    plan_daily_limit_bytes: Optional[int] = None,
) -> bool:
    policy = settings.traffic_policy
    if not policy.enabled:
        return False

    changed = _refresh_daily_baseline(subscription, snapshot, policy)
    daily_used_bytes = max(snapshot.total_bytes - subscription.daily_baseline_bytes, 0)
    daily_limit_bytes = plan_daily_limit_bytes or policy.daily_limit_bytes
    target_speed_limit = policy.throttled_speed_kbytes_per_second if daily_used_bytes >= daily_limit_bytes else 0

    if subscription.speed_limit_kbytes_per_second == target_speed_limit:
        return changed

    await panel.update_client_speed_limit(
        settings.xui.inbound_id,
        client_id=subscription.xui_client_id,
        speed_limit_kbytes_per_second=target_speed_limit,
    )
    subscription.speed_limit_kbytes_per_second = target_speed_limit
    return True


def _refresh_daily_baseline(
    subscription: Subscription,
    snapshot: TrafficSnapshot,
    policy: TrafficPolicySettings,
) -> bool:
    today = utc_now().astimezone(ZoneInfo(policy.timezone)).date().isoformat()
    if subscription.daily_traffic_date == today:
        return False
    subscription.daily_traffic_date = today
    subscription.daily_baseline_bytes = snapshot.total_bytes
    return True


def _get_plan_daily_limit_bytes(
    subscription: Subscription,
    plans: Optional[Mapping[str, PlanDefinition]],
) -> Optional[int]:
    if plans is None:
        return None
    plan = plans.get(subscription.plan_code)
    if plan is None:
        return None
    return plan.daily_limit_bytes


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
