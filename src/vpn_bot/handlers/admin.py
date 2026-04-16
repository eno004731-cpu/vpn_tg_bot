from __future__ import annotations

import logging
from html import escape
from typing import Optional, Union

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from vpn_bot.formatters import (
    format_admin_dashboard,
    format_admin_traffic_report,
    format_invoice_rejection,
)
from vpn_bot.keyboards import AdminInvoiceAction
from vpn_bot.models import Invoice, InvoiceStatus, Subscription, SubscriptionStatus
from vpn_bot.runtime import AppContext
from vpn_bot.services.payments import reject_invoice
from vpn_bot.services.subscriptions import activate_invoice, sync_active_subscriptions
from vpn_bot.services.users import ensure_user
from vpn_bot.utils import ensure_utc

router = Router(name="admin")


def _is_admin(message_or_callback: Union[Message, CallbackQuery], admin_ids: tuple[int, ...]) -> bool:
    user = message_or_callback.from_user
    return user is not None and user.id in admin_ids


@router.message(Command("admin"))
async def admin_dashboard(message: Message, app_context: AppContext) -> None:
    if not _is_admin(message, app_context.settings.app.admin_ids):
        return
    async with app_context.session_factory() as session:
        await ensure_user(session, message.from_user, app_context.settings.app.admin_ids)
        pending_count = await session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.status == InvoiceStatus.pending_review.value)
        )
        active_count = await session.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.status == SubscriptionStatus.active.value)
        )
        await session.commit()
    await message.answer(format_admin_dashboard(pending_count or 0, active_count or 0))


@router.message(Command("traffic_admin"))
async def traffic_admin(message: Message, app_context: AppContext) -> None:
    if not _is_admin(message, app_context.settings.app.admin_ids):
        return
    async with app_context.session_factory() as session:
        await ensure_user(session, message.from_user, app_context.settings.app.admin_ids)
        subscriptions = await sync_active_subscriptions(session, app_context.panel)
        await session.commit()
    await message.answer(format_admin_traffic_report(subscriptions))


@router.callback_query(AdminInvoiceAction.filter())
async def invoice_review(callback: CallbackQuery, callback_data: AdminInvoiceAction, app_context: AppContext) -> None:
    if not _is_admin(callback, app_context.settings.app.admin_ids):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback_data.action == "approve":
        await callback.answer("Подтверждаю оплату...")
        await _approve_invoice(callback, callback_data.invoice_id, app_context)
        return
    if callback_data.action == "reject":
        await callback.answer("Отклоняю платёж...")
        await _reject_invoice(callback, callback_data.invoice_id, app_context, note=None)
        return
    await callback.answer("Неизвестное действие.", show_alert=True)


@router.message(Command("approve"))
async def approve_command(message: Message, command: CommandObject, app_context: AppContext) -> None:
    if not _is_admin(message, app_context.settings.app.admin_ids):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: /approve <invoice_id>")
        return
    await _approve_invoice(message, int(command.args.strip()), app_context)


@router.message(Command("reject"))
async def reject_command(message: Message, command: CommandObject, app_context: AppContext) -> None:
    if not _is_admin(message, app_context.settings.app.admin_ids):
        return
    if not command.args:
        await message.answer("Использование: /reject <invoice_id> [причина]")
        return
    invoice_id_raw, *note_parts = command.args.split(maxsplit=1)
    if not invoice_id_raw.isdigit():
        await message.answer("Invoice ID должен быть числом.")
        return
    note = note_parts[0] if note_parts else None
    await _reject_invoice(message, int(invoice_id_raw), app_context, note)


async def _approve_invoice(target: Union[Message, CallbackQuery], invoice_id: int, app_context: AppContext) -> None:
    async with app_context.session_factory() as session:
        try:
            result = await activate_invoice(session, app_context.settings, app_context.panel, invoice_id)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to activate invoice %s", invoice_id)
            try:
                if isinstance(target, CallbackQuery):
                    await target.message.answer(
                        f"Не удалось активировать инвойс <code>{invoice_id}</code>: {escape(str(exc))}"
                    )
                else:
                    await target.answer(f"Не удалось активировать инвойс: {exc}")
            except TelegramAPIError:
                logging.exception("Failed to notify admin about invoice %s activation error", invoice_id)
            return

    access_text = "\n".join(
        [
            "<b>Оплата подтверждена</b>",
            f"Тариф: {escape(result.subscription.plan_title)}",
            f"Трафик: {result.subscription.traffic_limit_bytes} байт",
            f"Ссылка: <code>{escape(result.subscription.access_url)}</code>",
            f"Действует до: {ensure_utc(result.subscription.ends_at).astimezone().strftime('%Y-%m-%d %H:%M')}",
        ]
    )
    await target.bot.send_message(result.user.tg_id, access_text)

    confirmation = f"Инвойс <code>{invoice_id}</code> подтверждён."
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(confirmation)
    else:
        await target.answer(confirmation)


async def _reject_invoice(
    target: Union[Message, CallbackQuery],
    invoice_id: int,
    app_context: AppContext,
    note: Optional[str],
) -> None:
    async with app_context.session_factory() as session:
        invoice = await session.scalar(select(Invoice).where(Invoice.id == invoice_id))
        if invoice is None:
            if isinstance(target, CallbackQuery):
                await target.answer("Инвойс не найден.", show_alert=True)
            else:
                await target.answer("Инвойс не найден.")
            return
        reject_invoice(invoice, note)
        await session.commit()

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(format_invoice_rejection(invoice, note))
    else:
        await target.answer(format_invoice_rejection(invoice, note))
