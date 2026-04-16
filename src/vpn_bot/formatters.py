from __future__ import annotations

from html import escape
from typing import Iterable, Optional

from vpn_bot.models import Invoice, Subscription, User
from vpn_bot.utils import ensure_utc, format_bytes


def format_user_subscriptions(subscriptions: Iterable[Subscription]) -> str:
    parts: list[str] = []
    for subscription in subscriptions:
        parts.append(
            "\n".join(
                [
                    f"<b>{escape(subscription.plan_title)}</b>",
                    f"Использовано: {format_bytes(subscription.traffic_used_bytes)} / {format_bytes(subscription.traffic_limit_bytes)}",
                    f"Загрузка: {format_bytes(subscription.download_bytes)}",
                    f"Отдача: {format_bytes(subscription.upload_bytes)}",
                    f"Действует до: {ensure_utc(subscription.ends_at).astimezone().strftime('%Y-%m-%d %H:%M')}",
                    f"Ссылка: <code>{escape(subscription.access_url)}</code>",
                ]
            )
        )
    return "\n\n".join(parts)


def format_admin_traffic_report(subscriptions: Iterable[Subscription]) -> str:
    lines = ["<b>Активные подписки и трафик</b>"]
    ordered = sorted(subscriptions, key=lambda item: item.traffic_used_bytes, reverse=True)
    if not ordered:
        return "\n".join(lines + ["Активных подписок пока нет."])

    for item in ordered:
        lines.append(
            (
                f"- <code>{item.user.tg_id}</code> {escape(item.user.username or '-')} | "
                f"{escape(item.plan_title)} | {format_bytes(item.traffic_used_bytes)} / "
                f"{format_bytes(item.traffic_limit_bytes)} | "
                f"до {ensure_utc(item.ends_at).astimezone().strftime('%Y-%m-%d')}"
            )
        )
    return "\n".join(lines)


def format_admin_dashboard(pending_invoices: int, active_subscriptions: int) -> str:
    return "\n".join(
        [
            "<b>Админ-панель</b>",
            f"Платежей на проверке: <code>{pending_invoices}</code>",
            f"Активных подписок: <code>{active_subscriptions}</code>",
        ]
    )


def format_invoice_rejection(invoice: Invoice, note: Optional[str]) -> str:
    if note:
        return f"Инвойс <code>{invoice.id}</code> отклонён. Причина: {escape(note)}"
    return f"Инвойс <code>{invoice.id}</code> отклонён."


def format_user_tag(user: User) -> str:
    if user.username:
        return f"@{user.username}"
    return str(user.tg_id)
