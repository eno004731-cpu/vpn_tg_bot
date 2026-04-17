from __future__ import annotations

import logging
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
from vpn_bot.services.crypto import decrypt_value, encrypt_value
from vpn_bot.services.nodes import NodeRegistry
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


def encrypt_subscription_field(value: str, settings: Settings) -> str:
    return encrypt_value(value, settings.app.field_encryption_key) or value


def decrypt_subscription_field(value: str, settings: Optional[Settings]) -> str:
    if settings is None:
        return value
    return decrypt_value(value, settings.app.field_encryption_key) or value


def get_subscription_access_url(subscription: Subscription, settings: Settings) -> str:
    return decrypt_subscription_field(subscription.access_url, settings)


async def activate_invoice(
    session: AsyncSession, settings: Settings, nodes: NodeRegistry, invoice_id: int
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
    node = await nodes.select_node_for_new_subscription(session)
    panel = nodes.get_client(node.node_code)
    client_id = panel.generate_client_id()
    xui_email = build_xui_email(invoice.user.tg_id, invoice.id, invoice.plan_code)
    expires_at = now + timedelta(days=invoice.duration_days)
    provisioned = await panel.add_client(
        node.inbound_id,
        client_id=client_id,
        email=xui_email,
        traffic_limit_bytes=invoice.traffic_limit_bytes,
        expires_at=expires_at,
        flow=node.flow,
        telegram_user_id=invoice.user.tg_id,
        comment=invoice.plan_title,
    )
    subscription = Subscription(
        user_id=invoice.user_id,
        source_invoice_id=invoice.id,
        plan_code=invoice.plan_code,
        plan_title=invoice.plan_title,
        status=SubscriptionStatus.active.value,
        node_code=node.node_code,
        xui_client_id=encrypt_subscription_field(provisioned.client_id, settings),
        xui_email=encrypt_subscription_field(provisioned.email, settings),
        access_url=encrypt_subscription_field(provisioned.access_url, settings),
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
    nodes: NodeRegistry,
    user: User,
    *,
    plan_code: str,
    plan_title: str,
    duration_days: int,
    traffic_limit_bytes: int,
    source_invoice_id: Optional[int] = None,
) -> Subscription:
    now = utc_now()
    node = await nodes.select_node_for_new_subscription(session)
    panel = nodes.get_client(node.node_code)
    client_id = panel.generate_client_id()
    xui_email = build_manual_xui_email(user.tg_id, plan_code)
    expires_at = now + timedelta(days=duration_days)
    provisioned = await panel.add_client(
        node.inbound_id,
        client_id=client_id,
        email=xui_email,
        traffic_limit_bytes=traffic_limit_bytes,
        expires_at=expires_at,
        flow=node.flow,
        telegram_user_id=user.tg_id,
        comment=plan_title,
    )
    subscription = Subscription(
        user_id=user.id,
        source_invoice_id=source_invoice_id,
        plan_code=plan_code,
        plan_title=plan_title,
        status=SubscriptionStatus.active.value,
        node_code=node.node_code,
        xui_client_id=encrypt_subscription_field(provisioned.client_id, settings),
        xui_email=encrypt_subscription_field(provisioned.email, settings),
        access_url=encrypt_subscription_field(provisioned.access_url, settings),
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
    nodes: NodeRegistry,
    subscription_id: int,
) -> Subscription:
    subscription = await session.scalar(
        select(Subscription).options(selectinload(Subscription.user)).where(Subscription.id == subscription_id)
    )
    if subscription is None:
        raise ValueError("Подписка не найдена.")
    if subscription.status != SubscriptionStatus.active.value:
        raise ValueError("Подписка уже не активна.")

    node_code = subscription.node_code or settings.xui.node_code
    node = nodes.get_settings(node_code)
    panel = nodes.get_client(node.node_code)
    await panel.set_client_enabled(
        node.inbound_id,
        client_id=decrypt_subscription_field(subscription.xui_client_id, settings),
        enabled=False,
    )
    subscription.status = SubscriptionStatus.revoked.value
    subscription.speed_limit_kbytes_per_second = 0
    await session.commit()
    await session.refresh(subscription)
    return subscription


async def sync_active_subscriptions(
    session: AsyncSession,
    nodes: NodeRegistry,
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

    now = utc_now()
    changed = False
    subscriptions_by_node: dict[str, list[Subscription]] = {}

    for subscription in subscriptions:
        if (
            ensure_utc(subscription.ends_at) <= now
            or subscription.traffic_used_bytes >= subscription.traffic_limit_bytes
        ):
            if subscription.status != SubscriptionStatus.expired.value:
                subscription.status = SubscriptionStatus.expired.value
                changed = True
            continue
        node_code = subscription.node_code or (settings.xui.node_code if settings is not None else "main")
        subscriptions_by_node.setdefault(node_code, []).append(subscription)

    for node_code, node_subscriptions in subscriptions_by_node.items():
        try:
            node = nodes.get_settings(node_code)
            panel = nodes.get_client(node.node_code)
            traffic_map = await panel.fetch_traffic_map()
        except Exception:  # noqa: BLE001
            logging.exception("Traffic sync failed for node %s", node_code)
            continue

        for subscription in node_subscriptions:
            snapshot = traffic_map.get(decrypt_subscription_field(subscription.xui_email, settings))
            if snapshot is None:
                continue
            subscription.upload_bytes = snapshot.upload_bytes
            subscription.download_bytes = snapshot.download_bytes
            subscription.traffic_used_bytes = snapshot.total_bytes
            subscription.last_synced_at = now
            changed = True
            if (
                subscription.traffic_used_bytes >= subscription.traffic_limit_bytes
                and subscription.status != SubscriptionStatus.expired.value
            ):
                subscription.status = SubscriptionStatus.expired.value
                changed = True
                continue
            if settings is not None and await _apply_daily_traffic_policy(
                subscription,
                snapshot,
                panel,
                settings,
                node_inbound_id=node.inbound_id,
                plan_daily_limit_bytes=_get_plan_daily_limit_bytes(subscription, plans),
            ):
                changed = True

    if changed:
        await session.commit()
    return subscriptions


async def _apply_daily_traffic_policy(
    subscription: Subscription,
    snapshot: TrafficSnapshot,
    panel: XUIClient,
    settings: Settings,
    *,
    node_inbound_id: Optional[int] = None,
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
        node_inbound_id or settings.xui.inbound_id,
        client_id=decrypt_subscription_field(subscription.xui_client_id, settings),
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
                        InvoiceStatus.paid_pending_provision.value,
                        InvoiceStatus.provision_failed.value,
                    ]
                ),
            )
            .order_by(Invoice.created_at.desc())
        )
    )


def format_subscription_lines(subscriptions: Iterable[Subscription], settings: Optional[Settings] = None) -> str:
    blocks: list[str] = []
    for item in subscriptions:
        blocks.append(
            "\n".join(
                [
                    f"<b>{item.plan_title}</b>",
                    f"Трафик: <code>{item.traffic_used_bytes}</code> / <code>{item.traffic_limit_bytes}</code> байт",
                    f"До: {ensure_utc(item.ends_at).astimezone().strftime('%Y-%m-%d %H:%M')}",
                    f"Ссылка: <code>{decrypt_subscription_field(item.access_url, settings)}</code>",
                ]
            )
        )
    return "\n\n".join(blocks)
