from __future__ import annotations

import logging
from html import escape
from typing import Optional, Union

from aiogram import Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from vpn_bot.formatters import (
    format_admin_dashboard,
    format_admin_help,
    format_admin_nodes_report,
    format_admin_traffic_report,
    format_invoice_rejection,
    format_traffic_usage,
)
from vpn_bot.keyboards import (
    AdminCustomGrantAction,
    AdminGrantPlan,
    AdminInvoiceAction,
    AdminInvoicesPage,
    AdminSubscriptionAction,
    AdminUserAction,
    AdminUsersPage,
    admin_custom_grant_keyboard,
    admin_grant_plans_keyboard,
    admin_invoices_page_keyboard,
    admin_user_keyboard,
    admin_user_search_back_keyboard,
    admin_users_keyboard,
    format_custom_plan_builder,
)
from vpn_bot.models import Invoice, InvoiceStatus, Subscription, SubscriptionStatus, User
from vpn_bot.runtime import AppContext
from vpn_bot.services.custom_plans import (
    build_custom_plan,
    clamp_custom_days,
    clamp_custom_devices,
    normalize_custom_kind,
)
from vpn_bot.services.jobs import schedule_invoice_provisioning
from vpn_bot.services.payments import reject_invoice
from vpn_bot.services.subscriptions import (
    get_subscription_access_url,
    provision_subscription_for_user,
    revoke_subscription,
    sync_active_subscriptions,
)
from vpn_bot.services.users import ensure_user
from vpn_bot.utils import ensure_utc, format_bytes

router = Router(name="admin")
ADMIN_USERS_PAGE_SIZE = 10
ADMIN_INVOICES_PAGE_SIZE = 10
OPEN_INVOICE_STATUSES = (
    InvoiceStatus.awaiting_transfer.value,
    InvoiceStatus.pending_review.value,
    InvoiceStatus.paid_pending_provision.value,
    InvoiceStatus.provision_failed.value,
)


async def _answer_callback(callback: CallbackQuery, text: str = "", *, show_alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest:
        logging.debug("Skipped callback answer because Telegram no longer accepts this query", exc_info=True)


def _is_admin(message_or_callback: Union[Message, CallbackQuery], admin_ids: tuple[int, ...]) -> bool:
    user = message_or_callback.from_user
    return user is not None and user.id in admin_ids


@router.message(Command("admin"))
async def admin_dashboard(message: Message, command: CommandObject, app_context: AppContext) -> None:
    if not _is_admin(message, app_context.settings.app.admin_ids):
        return
    admin_args = (command.args or "").strip().lower()
    if admin_args in {"help", "commands", "команды"}:
        await message.answer(format_admin_help())
        return
    if admin_args in {"invoices", "invoice", "unpaid", "инвойсы", "счета"}:
        await _send_open_invoices(message, app_context, page=0)
        return
    if admin_args in {"nodes", "node", "ноды", "серверы"}:
        await _send_nodes_report(message, app_context)
        return
    if admin_args.startswith(("users", "пользователи")):
        _, _, query = admin_args.partition(" ")
        await _send_users_list(message, app_context, page=0, query=query.strip() or None)
        return
    if admin_args:
        await message.answer("Неизвестная админ-команда. Использование: /admin help")
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


@router.message(Command("users"))
async def users_command(message: Message, command: CommandObject, app_context: AppContext) -> None:
    if not _is_admin(message, app_context.settings.app.admin_ids):
        return
    query = (command.args or "").strip() or None
    await _send_users_list(message, app_context, page=0, query=query)


@router.message(Command("invoices"))
async def invoices_command(message: Message, app_context: AppContext) -> None:
    if not _is_admin(message, app_context.settings.app.admin_ids):
        return
    await _send_open_invoices(message, app_context, page=0)


@router.message(Command("nodes"))
async def nodes_command(message: Message, app_context: AppContext) -> None:
    if not _is_admin(message, app_context.settings.app.admin_ids):
        return
    await _send_nodes_report(message, app_context)


@router.message(Command("traffic_admin"))
async def traffic_admin(message: Message, app_context: AppContext) -> None:
    if not _is_admin(message, app_context.settings.app.admin_ids):
        return
    async with app_context.session_factory() as session:
        await ensure_user(session, message.from_user, app_context.settings.app.admin_ids)
        subscriptions = await sync_active_subscriptions(
            session, app_context.nodes, app_context.settings, app_context.plans
        )
        await session.commit()
    await message.answer(format_admin_traffic_report(subscriptions))


@router.callback_query(AdminInvoicesPage.filter())
async def invoices_page(callback: CallbackQuery, callback_data: AdminInvoicesPage, app_context: AppContext) -> None:
    if not _is_admin(callback, app_context.settings.app.admin_ids):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await _send_open_invoices(callback, app_context, page=callback_data.page)
    await callback.answer()


@router.callback_query(AdminUsersPage.filter())
async def users_page(callback: CallbackQuery, callback_data: AdminUsersPage, app_context: AppContext) -> None:
    if not _is_admin(callback, app_context.settings.app.admin_ids):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await _send_users_list(callback, app_context, page=callback_data.page)
    await callback.answer()


@router.callback_query(AdminUserAction.filter())
async def user_action(callback: CallbackQuery, callback_data: AdminUserAction, app_context: AppContext) -> None:
    if not _is_admin(callback, app_context.settings.app.admin_ids):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback_data.action == "view":
        await _show_user_detail(callback, app_context, callback_data.user_id, callback_data.page)
        await callback.answer()
        return
    if callback_data.action == "grant":
        await _show_grant_plan_menu(callback, app_context, callback_data.user_id, callback_data.page)
        await callback.answer()
        return
    await callback.answer("Неизвестное действие.", show_alert=True)


@router.callback_query(AdminGrantPlan.filter())
async def grant_plan(callback: CallbackQuery, callback_data: AdminGrantPlan, app_context: AppContext) -> None:
    if not _is_admin(callback, app_context.settings.app.admin_ids):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    plan = app_context.plans.get(callback_data.plan_code)
    if plan is None or not plan.provision_access:
        await callback.answer("Тариф не найден или не выдаёт доступ.", show_alert=True)
        return

    await _grant_access_with_plan(callback, callback_data.user_id, callback_data.page, plan, app_context)


@router.callback_query(AdminCustomGrantAction.filter())
async def custom_grant_action(
    callback: CallbackQuery, callback_data: AdminCustomGrantAction, app_context: AppContext
) -> None:
    if not _is_admin(callback, app_context.settings.app.admin_ids):
        await _answer_callback(callback, "Недостаточно прав.", show_alert=True)
        return

    if callback_data.action == "back":
        await _show_grant_plan_menu(callback, app_context, callback_data.user_id, callback_data.page)
        await _answer_callback(callback)
        return

    try:
        kind = normalize_custom_kind(callback_data.kind)
        current_days = clamp_custom_days(callback_data.days)
        current_devices = clamp_custom_devices(callback_data.devices)
        days, devices = _apply_custom_plan_action(callback_data.action, callback_data.days, callback_data.devices)
        plan = build_custom_plan(kind, days, devices)
    except ValueError:
        await _answer_callback(callback, "Не удалось собрать тариф.", show_alert=True)
        return

    if callback_data.action == "grant":
        await _grant_access_with_plan(callback, callback_data.user_id, callback_data.page, plan, app_context)
        return

    if callback_data.action != "show" and (days, devices) == (current_days, current_devices):
        await _answer_callback(callback, _custom_plan_noop_message(callback_data.action, days, devices))
        return

    async with app_context.session_factory() as session:
        user = await session.scalar(select(User).where(User.id == callback_data.user_id))
    if user is None:
        await _answer_callback(callback, "Пользователь не найден.", show_alert=True)
        return

    try:
        await callback.message.edit_text(
            "\n".join(
                [
                    f"Выдача доступа для <code>{user.tg_id}</code> {_format_user_name(user)}:",
                    "",
                    format_custom_plan_builder(kind, days, devices),
                ]
            ),
            reply_markup=admin_custom_grant_keyboard(kind, days, devices, user.id, callback_data.page),
        )
    except TelegramBadRequest as exc:
        if _is_message_not_modified(exc):
            await _answer_callback(callback)
            return
        raise
    await _answer_callback(callback)


@router.callback_query(AdminSubscriptionAction.filter())
async def subscription_action(
    callback: CallbackQuery, callback_data: AdminSubscriptionAction, app_context: AppContext
) -> None:
    if not _is_admin(callback, app_context.settings.app.admin_ids):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback_data.action != "revoke":
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    async with app_context.session_factory() as session:
        try:
            subscription = await revoke_subscription(
                session,
                app_context.settings,
                app_context.nodes,
                callback_data.subscription_id,
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to revoke subscription %s", callback_data.subscription_id)
            await callback.answer(f"Не удалось забрать доступ: {exc}", show_alert=True)
            return
        user_tg_id = subscription.user.tg_id

    await _safe_send_message(
        callback.bot,
        user_tg_id,
        f"Доступ по тарифу <b>{escape(subscription.plan_title)}</b> отозван администратором.",
    )
    await callback.answer("Доступ отозван")
    await _show_user_detail(callback, app_context, callback_data.user_id, callback_data.page)


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
            await schedule_invoice_provisioning(
                session,
                app_context.settings,
                app_context.nodes,
                invoice_id,
                app_context.plans,
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to schedule invoice %s provisioning", invoice_id)
            try:
                if isinstance(target, CallbackQuery):
                    await target.message.answer(
                        f"Не удалось поставить инвойс <code>{invoice_id}</code> в очередь выдачи: {escape(str(exc))}"
                    )
                else:
                    await target.answer(f"Не удалось поставить инвойс в очередь выдачи: {exc}")
            except TelegramAPIError:
                logging.exception("Failed to notify admin about invoice %s provisioning error", invoice_id)
            return

    confirmation = (
        f"Инвойс <code>{invoice_id}</code> подтверждён и поставлен в очередь выдачи. "
        "Пользователь получит ссылку после обработки worker."
    )
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
        try:
            reject_invoice(invoice, note)
        except ValueError as exc:
            if isinstance(target, CallbackQuery):
                await target.answer(str(exc), show_alert=True)
            else:
                await target.answer(str(exc))
            return
        await session.commit()

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(format_invoice_rejection(invoice, note))
    else:
        await target.answer(format_invoice_rejection(invoice, note))


async def _send_open_invoices(
    target: Union[Message, CallbackQuery],
    app_context: AppContext,
    *,
    page: int = 0,
) -> None:
    page = max(page, 0)
    async with app_context.session_factory() as session:
        total = await session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.status.in_(OPEN_INVOICE_STATUSES))
        )
        invoices = list(
            await session.scalars(
                select(Invoice)
                .where(Invoice.status.in_(OPEN_INVOICE_STATUSES))
                .options(selectinload(Invoice.user))
                .order_by(Invoice.created_at.desc())
                .offset(page * ADMIN_INVOICES_PAGE_SIZE)
                .limit(ADMIN_INVOICES_PAGE_SIZE + 1)
            )
        )

    has_next = len(invoices) > ADMIN_INVOICES_PAGE_SIZE
    invoices = invoices[:ADMIN_INVOICES_PAGE_SIZE]
    text = _format_open_invoices_page(invoices, page=page, total=total or 0)
    keyboard = admin_invoices_page_keyboard(page, has_prev=page > 0, has_next=has_next)
    await _send_or_edit(target, text, keyboard)


async def _send_nodes_report(message: Message, app_context: AppContext) -> None:
    async with app_context.session_factory() as session:
        statuses = await app_context.nodes.collect_statuses(session)
    await message.answer(format_admin_nodes_report(statuses))


def _format_open_invoices_page(invoices: list[Invoice], *, page: int, total: int) -> str:
    lines = [
        "<b>Неоплаченные инвойсы</b>",
        f"Всего: <code>{total}</code>",
        f"Страница: <code>{page + 1}</code>",
    ]
    if not invoices:
        return "\n".join(lines + ["", "Открытых инвойсов нет."])

    lines.extend(["", "<code>ID</code> | статус | сумма | пользователь | до"])
    for invoice in invoices:
        lines.append(_format_open_invoice_line(invoice))
    lines.extend(["", "Подтвердить: <code>/approve ID</code>", "Отклонить: <code>/reject ID причина</code>"])
    return "\n".join(lines)


def _format_open_invoice_line(invoice: Invoice) -> str:
    user = invoice.user
    if user is None:
        user_label = "-"
    elif user.username:
        user_label = f"@{user.username}"
    else:
        user_label = str(user.tg_id)
    expires_at = ensure_utc(invoice.expires_at).astimezone().strftime("%Y-%m-%d %H:%M")
    return (
        f"<code>{invoice.id}</code> | {escape(invoice.status)} | "
        f"{invoice.amount_rub} ₽ | {escape(user_label)} | {expires_at}"
    )


async def _send_users_list(
    target: Union[Message, CallbackQuery],
    app_context: AppContext,
    *,
    page: int = 0,
    query: Optional[str] = None,
) -> None:
    page = max(page, 0)
    condition = _build_user_search_condition(query)

    async with app_context.session_factory() as session:
        total_query = select(func.count()).select_from(User)
        users_query = (
            select(User)
            .options(selectinload(User.subscriptions))
            .order_by(User.id.desc())
            .offset(page * ADMIN_USERS_PAGE_SIZE)
            .limit(ADMIN_USERS_PAGE_SIZE + 1)
        )
        if condition is not None:
            total_query = total_query.where(condition)
            users_query = users_query.where(condition)
        total = await session.scalar(total_query) or 0
        users = list(await session.scalars(users_query))

    has_next = query is None and len(users) > ADMIN_USERS_PAGE_SIZE
    users = users[:ADMIN_USERS_PAGE_SIZE]
    labels = [(_user.id, _format_user_button_label(_user)) for _user in users]
    title = _format_users_page_title(page, total, query)
    if not users:
        title += "\n\nНичего не найдено."
        keyboard = admin_user_search_back_keyboard()
    else:
        keyboard = admin_users_keyboard(labels, page, has_prev=query is None and page > 0, has_next=has_next)
    await _send_or_edit(target, title, keyboard)


async def _show_user_detail(
    callback: CallbackQuery,
    app_context: AppContext,
    user_id: int,
    page: int,
) -> None:
    async with app_context.session_factory() as session:
        user = await session.scalar(select(User).where(User.id == user_id).options(selectinload(User.subscriptions)))
    if user is None:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    active_ids = [
        subscription.id
        for subscription in sorted(user.subscriptions, key=lambda item: item.id, reverse=True)
        if subscription.status == SubscriptionStatus.active.value
    ]
    await callback.message.edit_text(
        _format_user_detail(user),
        reply_markup=admin_user_keyboard(user.id, page, active_ids),
    )


async def _show_grant_plan_menu(
    callback: CallbackQuery,
    app_context: AppContext,
    user_id: int,
    page: int,
) -> None:
    async with app_context.session_factory() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    plans = [plan for plan in app_context.plans.values() if plan.provision_access]
    await callback.message.edit_text(
        f"Выберите тариф для <code>{user.tg_id}</code> {_format_user_name(user)}:",
        reply_markup=admin_grant_plans_keyboard(user.id, page, plans),
    )


async def _grant_access_with_plan(
    callback: CallbackQuery,
    user_id: int,
    page: int,
    plan,
    app_context: AppContext,
) -> None:
    async with app_context.session_factory() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        if user is None:
            await _answer_callback(callback, "Пользователь не найден.", show_alert=True)
            return
        try:
            subscription = await provision_subscription_for_user(
                session,
                app_context.settings,
                app_context.nodes,
                user,
                plan_code=plan.code,
                plan_title=plan.title,
                duration_days=plan.duration_days,
                traffic_limit_bytes=plan.traffic_limit_bytes,
                device_limit=plan.device_limit,
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to grant access to user %s", user_id)
            await _answer_callback(callback, f"Не удалось выдать доступ: {exc}", show_alert=True)
            return

    await _safe_send_message(
        callback.bot,
        user.tg_id,
        "\n".join(
            [
                "<b>Администратор выдал доступ</b>",
                f"Тариф: {escape(subscription.plan_title)}",
                f"Ссылка: <code>{escape(get_subscription_access_url(subscription, app_context.settings))}</code>",
                f"Действует до: {ensure_utc(subscription.ends_at).astimezone().strftime('%Y-%m-%d %H:%M')}",
            ]
        ),
    )
    await _answer_callback(callback, "Доступ выдан")
    await _show_user_detail(callback, app_context, user_id, page)


def _apply_custom_plan_action(action: str, days: int, devices: int) -> tuple[int, int]:
    days = clamp_custom_days(days)
    devices = clamp_custom_devices(devices)
    if action in {"show", "grant"}:
        return days, devices
    if action.startswith("p") and action[1:].isdigit():
        return clamp_custom_days(int(action[1:])), devices

    day_actions = {
        "dm30": -30,
        "dm7": -7,
        "dm1": -1,
        "dp1": 1,
        "dp7": 7,
        "dp30": 30,
    }
    device_actions = {"um1": -1, "up1": 1}
    if action in day_actions:
        return clamp_custom_days(days + day_actions[action]), devices
    if action in device_actions:
        return days, clamp_custom_devices(devices + device_actions[action])
    raise ValueError("Некорректное действие конструктора.")


def _custom_plan_noop_message(action: str, days: int, devices: int) -> str:
    if action.startswith("dp"):
        return f"Уже максимум: {days} дней."
    if action.startswith("dm"):
        return f"Уже минимум: {days} день."
    if action == "up1":
        return f"Уже максимум: {devices} устройств."
    if action == "um1":
        return f"Уже минимум: {devices} устройство."
    if action.startswith("p"):
        return "Уже выбран этот срок."
    return "Уже выбрано."


def _is_message_not_modified(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


def _build_user_search_condition(query: Optional[str]):
    if not query:
        return None
    normalized = query.strip().lstrip("@")
    if not normalized:
        return None
    if normalized.isdigit():
        value = int(normalized)
        return or_(User.id == value, User.tg_id == value)
    pattern = f"%{normalized.lower()}%"
    return or_(
        func.lower(User.username).like(pattern),
        func.lower(User.full_name).like(pattern),
    )


def _format_users_page_title(page: int, total: int, query: Optional[str]) -> str:
    lines = [
        "<b>Пользователи</b>",
        f"Всего: <code>{total}</code>",
    ]
    if query:
        lines.append(f"Поиск: <code>{escape(query)}</code>")
    lines.append(f"Страница: <code>{page + 1}</code>")
    return "\n".join(lines)


def _format_user_button_label(user: User) -> str:
    active = [item for item in user.subscriptions if item.status == SubscriptionStatus.active.value]
    traffic_used = sum(item.traffic_used_bytes for item in active)
    name = _plain_user_name(user)
    return f"{name} | {user.tg_id} | a{len(active)} | {format_bytes(traffic_used)}"


def _format_user_detail(user: User) -> str:
    subscriptions = sorted(user.subscriptions, key=lambda item: item.id, reverse=True)
    active = [item for item in subscriptions if item.status == SubscriptionStatus.active.value]
    lines = [
        "<b>Пользователь</b>",
        f"TG ID: <code>{user.tg_id}</code>",
        f"Username: {escape('@' + user.username) if user.username else '-'}",
        f"Имя: {escape(user.full_name or '-')}",
        f"Активных доступов: <code>{len(active)}</code>",
    ]
    if not subscriptions:
        return "\n".join(lines + ["", "Подписок пока нет."])

    lines.append("")
    lines.append("<b>Подписки</b>")
    for item in subscriptions[:8]:
        lines.append(
            (
                f"#{item.id} {escape(item.status)} | {escape(item.plan_title)} | "
                f"node {escape(item.node_code or '-')} | "
                f"{format_traffic_usage(item.traffic_used_bytes, item.traffic_limit_bytes)} | "
                f"до {ensure_utc(item.ends_at).astimezone().strftime('%Y-%m-%d')}"
            )
        )
    if len(subscriptions) > 8:
        lines.append(f"...ещё {len(subscriptions) - 8}")
    return "\n".join(lines)


def _plain_user_name(user: User) -> str:
    if user.username:
        value = f"@{user.username}"
    elif user.full_name:
        value = user.full_name
    else:
        value = "no name"
    return value[:16]


def _format_user_name(user: User) -> str:
    return escape(_plain_user_name(user))


async def _send_or_edit(target: Union[Message, CallbackQuery], text: str, keyboard) -> None:
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


async def _safe_send_message(bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        logging.exception("Failed to send message to %s", chat_id)
