from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any, Mapping, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vpn_bot.config import PlanDefinition, Settings
from vpn_bot.models import Invoice, InvoiceStatus, Job, JobStatus, JobType, Subscription, SubscriptionStatus
from vpn_bot.services.crypto import decrypt_value, encrypt_value
from vpn_bot.services.nodes import NodeRegistry
from vpn_bot.services.subscriptions import build_xui_email, get_plan_device_limit
from vpn_bot.services.xui import ProvisionedClient
from vpn_bot.utils import ensure_utc, utc_now

MAX_JOB_ATTEMPTS = 10


@dataclass(frozen=True)
class ProvisioningResult:
    invoice: Invoice
    subscription: Subscription


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _json_loads(payload: Optional[str]) -> dict[str, Any]:
    if not payload:
        return {}
    return json.loads(payload)


async def create_job_once(
    session: AsyncSession,
    *,
    job_type: JobType,
    idempotency_key: str,
    payload: dict[str, Any],
    invoice_id: Optional[int] = None,
    subscription_id: Optional[int] = None,
    user_id: Optional[int] = None,
    run_after: Optional[datetime] = None,
) -> Job:
    existing = await session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    job = Job(
        type=job_type.value,
        status=JobStatus.pending.value,
        idempotency_key=idempotency_key,
        payload=_json_dumps(payload),
        invoice_id=invoice_id,
        subscription_id=subscription_id,
        user_id=user_id,
        max_attempts=MAX_JOB_ATTEMPTS,
        run_after=run_after or utc_now(),
    )
    session.add(job)
    await session.flush()
    return job


async def schedule_invoice_provisioning(
    session: AsyncSession,
    settings: Settings,
    nodes: NodeRegistry,
    invoice_id: int,
    plans: Optional[Mapping[str, PlanDefinition]] = None,
) -> Job:
    invoice = await session.scalar(select(Invoice).options(selectinload(Invoice.user)).where(Invoice.id == invoice_id))
    if invoice is None:
        raise ValueError("Инвойс не найден.")
    if invoice.status == InvoiceStatus.paid.value:
        subscription = await session.scalar(select(Subscription).where(Subscription.source_invoice_id == invoice.id))
        if subscription is None:
            raise ValueError("Инвойс уже оплачен, но подписка не найдена.")
        return await create_job_once(
            session,
            job_type=JobType.send_access_message,
            idempotency_key=f"send-access:subscription:{subscription.id}",
            payload={"subscription_id": subscription.id},
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            user_id=invoice.user_id,
        )
    if invoice.status not in {
        InvoiceStatus.awaiting_transfer.value,
        InvoiceStatus.pending_review.value,
        InvoiceStatus.paid_pending_provision.value,
        InvoiceStatus.provision_failed.value,
    }:
        raise ValueError("Инвойс нельзя активировать в текущем статусе.")

    job = await session.scalar(select(Job).where(Job.idempotency_key == f"provision:invoice:{invoice.id}"))
    if job is None:
        node = await nodes.select_node_for_new_subscription(session)
        client_id = nodes.get_client(node.node_code).generate_client_id()
        payload = {
            "invoice_id": invoice.id,
            "node_code": node.node_code,
            "client_id": client_id,
            "xui_email": build_xui_email(invoice.user.tg_id, invoice.id, invoice.plan_code),
            "device_limit": get_plan_device_limit(invoice.plan_code, plans),
        }
        job = await create_job_once(
            session,
            job_type=JobType.provision_access,
            idempotency_key=f"provision:invoice:{invoice.id}",
            payload=payload,
            invoice_id=invoice.id,
            user_id=invoice.user_id,
        )
    elif job.status == JobStatus.failed.value:
        job.status = JobStatus.pending.value
        job.run_after = utc_now()
        job.locked_at = None
        job.last_error = None

    invoice.status = InvoiceStatus.paid_pending_provision.value
    if invoice.paid_at is None:
        invoice.paid_at = utc_now()
    await session.commit()
    await session.refresh(job)
    return job


async def claim_next_job(session: AsyncSession) -> Optional[Job]:
    now = utc_now()
    stale_before = now - timedelta(minutes=10)
    statement = (
        select(Job)
        .where(
            or_(
                (Job.status == JobStatus.pending.value) & (Job.run_after <= now),
                (Job.status == JobStatus.running.value) & (Job.locked_at <= stale_before),
            )
        )
        .order_by(Job.run_after.asc(), Job.id.asc())
        .limit(1)
    )
    if session.bind and session.bind.url.get_backend_name() == "postgresql":
        statement = statement.with_for_update(skip_locked=True)

    job = await session.scalar(statement)
    if job is None:
        return None
    job.status = JobStatus.running.value
    job.locked_at = now
    job.attempts += 1
    await session.commit()
    await session.refresh(job)
    return job


async def process_one_job(
    session: AsyncSession,
    settings: Settings,
    nodes: NodeRegistry,
    bot: Bot,
    plans: Optional[Mapping[str, PlanDefinition]] = None,
) -> bool:
    job = await claim_next_job(session)
    if job is None:
        return False

    try:
        if job.type == JobType.provision_access.value:
            await provision_access_for_job(session, settings, nodes, job, plans)
        elif job.type == JobType.send_access_message.value:
            await send_access_message_for_job(session, settings, bot, job)
        else:
            raise ValueError(f"Неизвестный тип job: {job.type}")
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to process job %s", job.id)
        await mark_job_failed_or_retry(session, settings, bot, job, exc)
        return True

    job.status = JobStatus.succeeded.value
    job.last_error = None
    await session.commit()
    return True


async def provision_access_for_job(
    session: AsyncSession,
    settings: Settings,
    nodes: NodeRegistry,
    job: Job,
    plans: Optional[Mapping[str, PlanDefinition]] = None,
) -> ProvisioningResult:
    payload = _json_loads(job.payload)
    invoice_id = int(payload.get("invoice_id") or job.invoice_id)
    invoice = await session.scalar(select(Invoice).options(selectinload(Invoice.user)).where(Invoice.id == invoice_id))
    if invoice is None:
        raise ValueError("Инвойс не найден.")

    existing = await session.scalar(select(Subscription).where(Subscription.source_invoice_id == invoice.id))
    if existing is not None:
        await create_job_once(
            session,
            job_type=JobType.send_access_message,
            idempotency_key=f"send-access:subscription:{existing.id}",
            payload={"subscription_id": existing.id},
            invoice_id=invoice.id,
            subscription_id=existing.id,
            user_id=invoice.user_id,
        )
        invoice.status = InvoiceStatus.paid.value
        await session.commit()
        return ProvisioningResult(invoice=invoice, subscription=existing)

    node_code = str(payload["node_code"])
    node = nodes.get_settings(node_code)
    panel = nodes.get_client(node_code)
    client_id = str(payload["client_id"])
    xui_email = str(payload["xui_email"])
    device_limit = max(1, int(payload.get("device_limit") or get_plan_device_limit(invoice.plan_code, plans)))
    expires_at = utc_now() + timedelta(days=invoice.duration_days)

    provisioned = await _ensure_xui_client(
        panel,
        node.inbound_id,
        client_id=client_id,
        email=xui_email,
        traffic_limit_bytes=invoice.traffic_limit_bytes,
        expires_at=expires_at,
        flow=node.flow,
        telegram_user_id=invoice.user.tg_id,
        comment=invoice.plan_title,
        limit_ip=device_limit,
    )
    subscription = Subscription(
        user_id=invoice.user_id,
        source_invoice_id=invoice.id,
        plan_code=invoice.plan_code,
        plan_title=invoice.plan_title,
        status=SubscriptionStatus.active.value,
        node_code=node.node_code,
        xui_client_id=encrypt_value(provisioned.client_id, settings.app.field_encryption_key),
        xui_email=encrypt_value(provisioned.email, settings.app.field_encryption_key),
        access_url=encrypt_value(provisioned.access_url, settings.app.field_encryption_key),
        traffic_limit_bytes=invoice.traffic_limit_bytes,
        started_at=utc_now(),
        ends_at=expires_at,
    )
    session.add(subscription)
    await session.flush()
    invoice.status = InvoiceStatus.paid.value
    if invoice.paid_at is None:
        invoice.paid_at = utc_now()
    await create_job_once(
        session,
        job_type=JobType.send_access_message,
        idempotency_key=f"send-access:subscription:{subscription.id}",
        payload={"subscription_id": subscription.id},
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        user_id=invoice.user_id,
    )
    await session.commit()
    await session.refresh(subscription)
    return ProvisioningResult(invoice=invoice, subscription=subscription)


async def send_access_message_for_job(
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    job: Job,
) -> None:
    payload = _json_loads(job.payload)
    subscription_id = int(payload.get("subscription_id") or job.subscription_id)
    subscription = await session.scalar(
        select(Subscription).options(selectinload(Subscription.user)).where(Subscription.id == subscription_id)
    )
    if subscription is None:
        raise ValueError("Подписка не найдена.")
    if subscription.access_sent_at is not None:
        return

    access_url = decrypt_value(subscription.access_url, settings.app.field_encryption_key)
    text = "\n".join(
        [
            "<b>Оплата подтверждена</b>",
            f"Тариф: {escape(subscription.plan_title)}",
            f"Ссылка: <code>{escape(access_url or '')}</code>",
            f"Действует до: {ensure_utc(subscription.ends_at).astimezone().strftime('%Y-%m-%d %H:%M')}",
        ]
    )
    await bot.send_message(subscription.user.tg_id, text)
    subscription.access_sent_at = utc_now()
    await session.commit()


async def mark_job_failed_or_retry(
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    job: Job,
    exc: Exception,
) -> None:
    job.last_error = str(exc)
    job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.failed.value
        if job.invoice_id and job.type == JobType.provision_access.value:
            invoice = await session.get(Invoice, job.invoice_id)
            if invoice is not None and invoice.status == InvoiceStatus.paid_pending_provision.value:
                invoice.status = InvoiceStatus.provision_failed.value
        await session.commit()
        await notify_admins_about_failed_job(settings, bot, job, exc)
        return

    delay_seconds = min(3600, 2 ** min(job.attempts, 10))
    job.status = JobStatus.pending.value
    job.run_after = utc_now() + timedelta(seconds=delay_seconds)
    await session.commit()


async def notify_admins_about_failed_job(settings: Settings, bot: Bot, job: Job, exc: Exception) -> None:
    text = "\n".join(
        [
            "<b>Job failed</b>",
            f"ID: <code>{job.id}</code>",
            f"Type: <code>{escape(job.type)}</code>",
            f"Invoice: <code>{job.invoice_id or '-'}</code>",
            f"Attempts: <code>{job.attempts}</code>",
            f"Error: <code>{escape(str(exc)[:500])}</code>",
        ]
    )
    for admin_id in settings.app.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            logging.exception("Failed to notify admin %s about failed job %s", admin_id, job.id)


async def _ensure_xui_client(panel, inbound_id: int, **kwargs) -> ProvisionedClient:
    try:
        inbound = await panel.get_inbound(inbound_id)
        try:
            panel._find_client(inbound, kwargs["client_id"])
        except Exception:  # noqa: BLE001
            pass
        else:
            access_url = panel.build_vless_reality_link(
                inbound,
                client_id=kwargs["client_id"],
                email=kwargs["email"],
            )
            return ProvisionedClient(
                client_id=kwargs["client_id"],
                email=kwargs["email"],
                access_url=access_url,
            )
    except Exception:  # noqa: BLE001
        logging.debug("Could not pre-check existing 3x-ui client", exc_info=True)

    return await panel.add_client(inbound_id, **kwargs)
