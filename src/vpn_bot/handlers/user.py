from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from vpn_bot.formatters import format_user_subscriptions
from vpn_bot.keyboards import (
    InvoiceAction,
    PlanChoice,
    admin_invoice_keyboard,
    invoice_keyboard,
    main_menu,
    plans_keyboard,
)
from vpn_bot.models import Invoice, InvoiceStatus
from vpn_bot.runtime import AppContext
from vpn_bot.services.payments import (
    mark_invoice_pending_review,
    create_invoice,
    format_invoice_for_admin,
    format_invoice_for_user,
)
from vpn_bot.services.subscriptions import get_open_invoices_for_user, get_user_active_subscriptions, sync_active_subscriptions
from vpn_bot.services.users import ensure_user
from vpn_bot.utils import ensure_utc, utc_now


router = Router(name="user")


@router.message(CommandStart())
async def start_handler(message: Message, app_context: AppContext) -> None:
    async with app_context.session_factory() as session:
        await ensure_user(session, message.from_user, app_context.settings.app.admin_ids)
        await session.commit()
    await message.answer(
        (
            "Привет. Я могу оформить VPN-подписку, показать трафик и выдать ссылку "
            "для Hiddify или v2RayTun."
        ),
        reply_markup=main_menu(),
    )


@router.message(Command("buy"))
@router.message(F.text == "Купить подписку")
async def buy_handler(message: Message, app_context: AppContext) -> None:
    async with app_context.session_factory() as session:
        await ensure_user(session, message.from_user, app_context.settings.app.admin_ids)
        await session.commit()
    plans = list(app_context.plans.values())
    await message.answer("Выберите тариф:", reply_markup=plans_keyboard(plans))


@router.callback_query(PlanChoice.filter())
async def plan_selected(
    callback: CallbackQuery, callback_data: PlanChoice, app_context: AppContext
) -> None:
    plan = app_context.plans.get(callback_data.code)
    if plan is None:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    async with app_context.session_factory() as session:
        user = await ensure_user(session, callback.from_user, app_context.settings.app.admin_ids)
        invoice = await create_invoice(session, user, plan, app_context.settings.payment)

    await callback.message.answer(
        format_invoice_for_user(invoice, app_context.settings.payment),
        reply_markup=invoice_keyboard(invoice.id),
    )
    await callback.answer("Инвойс создан")


@router.callback_query(InvoiceAction.filter(F.action == "paid"))
async def invoice_paid(
    callback: CallbackQuery, callback_data: InvoiceAction, app_context: AppContext
) -> None:
    async with app_context.session_factory() as session:
        invoice = await session.scalar(
            select(Invoice).where(Invoice.id == callback_data.invoice_id)
        )
        if invoice is None or callback.from_user is None:
            await callback.answer("Инвойс не найден.", show_alert=True)
            return
        user = await ensure_user(session, callback.from_user, app_context.settings.app.admin_ids)
        if invoice.user_id != user.id:
            await callback.answer("Это не ваш инвойс.", show_alert=True)
            return
        if invoice.status == InvoiceStatus.pending_review.value:
            await callback.answer("Платёж уже отправлен админу на проверку.")
            return
        if ensure_utc(invoice.expires_at) <= utc_now():
            invoice.status = InvoiceStatus.expired.value
            await session.commit()
            await callback.answer("Срок инвойса уже закончился.", show_alert=True)
            return
        if invoice.status == InvoiceStatus.expired.value:
            await callback.answer("Срок инвойса уже закончился.", show_alert=True)
            return
        mark_invoice_pending_review(invoice)
        await session.commit()
        admin_text = format_invoice_for_admin(invoice, user)

    for admin_id in app_context.settings.app.admin_ids:
        await callback.bot.send_message(
            admin_id,
            admin_text,
            reply_markup=admin_invoice_keyboard(invoice.id),
        )

    await callback.answer("Передал платёж админу на проверку.")
    await callback.message.answer("Платёж отправлен на проверку. Как только подтвержу перевод, пришлю доступ.")


@router.message(Command("my"))
@router.message(F.text == "Моя подписка")
async def my_subscription(message: Message, app_context: AppContext) -> None:
    async with app_context.session_factory() as session:
        user = await ensure_user(session, message.from_user, app_context.settings.app.admin_ids)
        await sync_active_subscriptions(session, app_context.panel)
        subscriptions = await get_user_active_subscriptions(session, user.id)
        open_invoices = await get_open_invoices_for_user(session, user.id)
        await session.commit()

    if subscriptions:
        await message.answer(format_user_subscriptions(subscriptions))
        return

    if open_invoices:
        invoice = open_invoices[0]
        await message.answer(
            (
                "У вас есть незавершённый инвойс:\n"
                f"{format_invoice_for_user(invoice, app_context.settings.payment)}"
            ),
            reply_markup=invoice_keyboard(invoice.id),
        )
        return

    await message.answer("Активной подписки пока нет. Нажмите «Купить подписку».")


@router.message(F.text == "Помощь")
@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "\n".join(
            [
                "Команды:",
                "/buy - выбрать тариф",
                "/my - посмотреть активную подписку и трафик",
                "",
                "После оплаты бот отдаст ссылку формата VLESS + REALITY.",
            ]
        )
    )
