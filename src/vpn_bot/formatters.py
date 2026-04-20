from __future__ import annotations

from html import escape
from typing import Iterable, Optional

from vpn_bot.models import Invoice, Subscription, User
from vpn_bot.services.crypto import decrypt_value
from vpn_bot.services.nodes import NodeStatus
from vpn_bot.utils import ensure_utc, format_bytes


def format_traffic_limit(limit_bytes: int) -> str:
    """Render a traffic limit, treating zero as unlimited."""

    if limit_bytes <= 0:
        return "Безлимит"
    return format_bytes(limit_bytes)


def format_traffic_usage(used_bytes: int, limit_bytes: int) -> str:
    """Render used traffic together with the plan limit."""

    return f"{format_bytes(used_bytes)} / {format_traffic_limit(limit_bytes)}"


def format_user_subscriptions(subscriptions: Iterable[Subscription], field_encryption_key: Optional[str] = None) -> str:
    """Render active subscriptions for the /my user command."""

    parts: list[str] = []
    for subscription in subscriptions:
        traffic_usage = format_traffic_usage(subscription.traffic_used_bytes, subscription.traffic_limit_bytes)
        access_url = decrypt_value(subscription.access_url, field_encryption_key) or subscription.access_url
        parts.append(
            "\n".join(
                [
                    f"<b>{escape(subscription.plan_title)}</b>",
                    f"Использовано: {traffic_usage}",
                    f"Загрузка: {format_bytes(subscription.download_bytes)}",
                    f"Отдача: {format_bytes(subscription.upload_bytes)}",
                    f"Действует до: {ensure_utc(subscription.ends_at).astimezone().strftime('%Y-%m-%d %H:%M')}",
                    f"Ссылка: <code>{escape(access_url)}</code>",
                ]
            )
        )
    return "\n\n".join(parts)


def format_admin_traffic_report(subscriptions: Iterable[Subscription]) -> str:
    """Render active subscriptions ordered by traffic usage for admins."""

    lines = ["<b>Активные подписки и трафик</b>"]
    ordered = sorted(subscriptions, key=lambda item: item.traffic_used_bytes, reverse=True)
    if not ordered:
        return "\n".join(lines + ["Активных подписок пока нет."])

    for item in ordered:
        lines.append(
            (
                f"- <code>{item.user.tg_id}</code> {escape(item.user.username or '-')} | "
                f"{escape(item.plan_title)} | {format_bytes(item.traffic_used_bytes)} / "
                f"{format_traffic_limit(item.traffic_limit_bytes)} | "
                f"до {ensure_utc(item.ends_at).astimezone().strftime('%Y-%m-%d')}"
            )
        )
    return "\n".join(lines)


def format_admin_dashboard(pending_invoices: int, active_subscriptions: int) -> str:
    """Render the compact admin dashboard summary."""

    return "\n".join(
        [
            "<b>Админ-панель</b>",
            f"Платежей на проверке: <code>{pending_invoices}</code>",
            f"Активных подписок: <code>{active_subscriptions}</code>",
            "",
            "Команды: /admin help",
        ]
    )


def format_admin_help() -> str:
    """Render the admin command reference."""

    return "\n".join(
        [
            "<b>Админ-команды</b>",
            "/admin - сводка по платежам и активным подпискам",
            "/admin help - показать эту подсказку",
            "/admin invoices - все неоплаченные инвойсы",
            "/invoices - короткий алиас для инвойсов",
            "/admin users [username|id] - список и поиск пользователей",
            "/users [username|id] - короткий алиас для поиска",
            "/admin nodes - показать VPN-ноды и статус API",
            "/nodes - короткий алиас для нод",
            "/traffic_admin - показать пользователей и расход трафика",
            "/approve &lt;invoice_id&gt; - подтвердить оплату",
            "/reject &lt;invoice_id&gt; [причина] - отклонить оплату",
            "",
            "Инвойсы также можно подтверждать кнопками в сообщении о платеже.",
        ]
    )


def format_invoice_rejection(invoice: Invoice, note: Optional[str]) -> str:
    """Render a rejection confirmation for admin command/callback output."""

    if note:
        return f"Инвойс <code>{invoice.id}</code> отклонён. Причина: {escape(note)}"
    return f"Инвойс <code>{invoice.id}</code> отклонён."


def format_admin_nodes_report(statuses: Iterable[NodeStatus]) -> str:
    """Render VPN node health and load information for admins."""

    ordered = sorted(statuses, key=lambda item: item.node.node_code)
    lines = ["<b>VPN-ноды</b>"]
    if not ordered:
        return "\n".join(lines + ["Ноды не настроены."])

    for status in ordered:
        node = status.node
        enabled = "on" if node.enabled else "off"
        api = "ok" if status.api_ok else "error"
        lines.append(
            (
                f"- <code>{escape(node.node_code)}</code> {escape(node.display_name)} | "
                f"{enabled} | prio <code>{node.priority}</code> | "
                f"{escape(node.public_host)}:<code>{node.public_port}</code> | "
                f"active <code>{status.active_subscriptions}</code> | API {api}"
            )
        )
        if status.error:
            lines.append(f"  Ошибка: <code>{escape(status.error[:180])}</code>")
    return "\n".join(lines)


def format_user_tag(user: User) -> str:
    """Return the preferred short Telegram identifier for a user."""

    if user.username:
        return f"@{user.username}"
    return str(user.tg_id)
