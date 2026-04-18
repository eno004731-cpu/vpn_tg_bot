from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from sqlalchemy import select

from vpn_bot.config import PlanDefinition
from vpn_bot.formatters import format_user_subscriptions
from vpn_bot.keyboards import (
    InvoiceAction,
    PaymentMethodChoice,
    PlanChoice,
    admin_invoice_keyboard,
    invoice_keyboard,
    main_menu,
    payment_methods_keyboard,
    plans_keyboard,
)
from vpn_bot.models import Invoice, InvoiceStatus
from vpn_bot.runtime import AppContext
from vpn_bot.services.jobs import schedule_invoice_provisioning
from vpn_bot.services.payments import (
    build_stars_payload,
    build_stars_reference,
    create_invoice,
    create_stars_invoice_record,
    expire_open_invoice,
    format_invoice_for_admin,
    format_invoice_for_user,
    mark_invoice_pending_review,
    parse_stars_payload,
    user_has_paid_plan,
)
from vpn_bot.services.subscriptions import (
    get_open_invoices_for_user,
    get_user_active_subscriptions,
)
from vpn_bot.services.users import ensure_user
from vpn_bot.utils import ensure_utc, utc_now

router = Router(name="user")


def _one_time_plan_error() -> str:
    return "Этот тариф можно купить только один раз."


async def _user_already_bought_plan(session, telegram_user, admin_ids: tuple[int, ...], plan: PlanDefinition) -> bool:
    user = await ensure_user(session, telegram_user, admin_ids)
    if not plan.one_time_per_user:
        return False
    return await user_has_paid_plan(session, user.id, plan.code)


@router.message(CommandStart())
async def start_handler(message: Message, app_context: AppContext) -> None:
    async with app_context.session_factory() as session:
        await ensure_user(session, message.from_user, app_context.settings.app.admin_ids)
        await session.commit()
    await message.answer(
        ("Привет. Я могу оформить VPN-подписку, показать трафик и выдать ссылку для Hiddify или v2RayTun."),
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
async def plan_selected(callback: CallbackQuery, callback_data: PlanChoice, app_context: AppContext) -> None:
    plan = app_context.plans.get(callback_data.code)
    if plan is None:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    if not plan.supports_transfer and not plan.supports_stars:
        await callback.answer("У тарифа нет доступных способов оплаты.", show_alert=True)
        return

    await callback.message.answer(_format_plan_payment_choice(plan), reply_markup=payment_methods_keyboard(plan))
    await callback.answer()


@router.callback_query(PaymentMethodChoice.filter(F.method == "transfer"))
async def transfer_payment_selected(
    callback: CallbackQuery, callback_data: PaymentMethodChoice, app_context: AppContext
) -> None:
    plan = app_context.plans.get(callback_data.code)
    if plan is None or not plan.supports_transfer:
        await callback.answer("Оплата переводом для этого тарифа недоступна.", show_alert=True)
        return

    async with app_context.session_factory() as session:
        user = await ensure_user(session, callback.from_user, app_context.settings.app.admin_ids)
        invoice = await create_invoice(session, user, plan, app_context.settings.payment)

    await callback.message.answer(
        format_invoice_for_user(invoice, app_context.settings.payment),
        reply_markup=invoice_keyboard(invoice.id),
    )
    await callback.answer("Инвойс создан")


@router.callback_query(PaymentMethodChoice.filter(F.method == "stars"))
async def stars_payment_selected(
    callback: CallbackQuery, callback_data: PaymentMethodChoice, app_context: AppContext
) -> None:
    plan = app_context.plans.get(callback_data.code)
    if plan is None or not plan.supports_stars or plan.price_stars is None:
        await callback.answer("Оплата Stars для этого тарифа недоступна.", show_alert=True)
        return
    if callback.from_user is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    async with app_context.session_factory() as session:
        if await _user_already_bought_plan(
            session,
            callback.from_user,
            app_context.settings.app.admin_ids,
            plan,
        ):
            await session.commit()
            await callback.answer(_one_time_plan_error(), show_alert=True)
            return
        await session.commit()

    description = plan.description or plan.title
    if not plan.provision_access:
        description = "Тестовая оплата Telegram Stars. VPN-доступ не выдаётся."

    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=plan.title,
        description=description,
        payload=build_stars_payload(plan.code, callback.from_user.id),
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=plan.title, amount=plan.price_stars)],
    )
    await callback.answer("Открыл оплату Stars")


@router.pre_checkout_query()
async def stars_pre_checkout(pre_checkout: PreCheckoutQuery, app_context: AppContext) -> None:
    try:
        payload = parse_stars_payload(pre_checkout.invoice_payload)
    except ValueError:
        await pre_checkout.answer(ok=False, error_message="Неизвестный платеж.")
        return

    plan = app_context.plans.get(payload.plan_code)
    if plan is None or not plan.supports_stars or plan.price_stars is None:
        await pre_checkout.answer(ok=False, error_message="Тариф больше недоступен.")
        return
    if pre_checkout.currency != "XTR" or pre_checkout.total_amount != plan.price_stars:
        await pre_checkout.answer(ok=False, error_message="Сумма платежа изменилась. Создайте оплату заново.")
        return
    if pre_checkout.from_user.id != payload.user_tg_id:
        await pre_checkout.answer(ok=False, error_message="Платеж создан для другого пользователя.")
        return

    async with app_context.session_factory() as session:
        if await _user_already_bought_plan(
            session,
            pre_checkout.from_user,
            app_context.settings.app.admin_ids,
            plan,
        ):
            await session.commit()
            await pre_checkout.answer(ok=False, error_message=_one_time_plan_error())
            return
        await session.commit()

    await pre_checkout.answer(ok=True)


@router.message(F.successful_payment)
async def stars_successful_payment(message: Message, app_context: AppContext) -> None:
    payment = message.successful_payment
    if payment is None:
        return
    try:
        payload = parse_stars_payload(payment.invoice_payload)
    except ValueError:
        return

    plan = app_context.plans.get(payload.plan_code)
    if message.from_user is None or plan is None or message.from_user.id != payload.user_tg_id:
        return

    if not plan.provision_access:
        await message.answer("Тестовая оплата Stars прошла. VPN-доступ по этому пункту не выдаётся.")
        return

    if payment.currency != "XTR" or payment.total_amount != plan.price_stars:
        await message.answer("Оплата Stars прошла, но сумма не совпала с тарифом. Напишите администратору.")
        return

    reference_code = build_stars_reference(payment.telegram_payment_charge_id)
    async with app_context.session_factory() as session:
        user = await ensure_user(session, message.from_user, app_context.settings.app.admin_ids)
        invoice = await session.scalar(select(Invoice).where(Invoice.reference_code == reference_code))
        if invoice is None and plan.one_time_per_user and await user_has_paid_plan(session, user.id, plan.code):
            await session.commit()
            await _notify_admins_about_stars_schedule_error(
                message,
                app_context,
                reference_code,
                RuntimeError(f"Повторная покупка одноразового тарифа {plan.code}"),
            )
            await message.answer(
                "Оплата Stars получена, но этот тариф уже был куплен раньше. "
                "Администратор уже видит ситуацию и поможет вручную."
            )
            return
        if invoice is None:
            invoice = await create_stars_invoice_record(
                session,
                user,
                plan,
                payment.telegram_payment_charge_id,
                payment.total_amount,
            )
        await session.flush()
        try:
            await schedule_invoice_provisioning(
                session,
                app_context.settings,
                app_context.nodes,
                invoice.id,
                app_context.plans,
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to schedule Stars payment %s provisioning", reference_code)
            invoice.status = InvoiceStatus.provision_failed.value
            if invoice.paid_at is None:
                invoice.paid_at = utc_now()
            await session.commit()
            await _notify_admins_about_stars_schedule_error(message, app_context, reference_code, exc)
            await message.answer(
                "\n".join(
                    [
                        "<b>Оплата Stars подтверждена</b>",
                        f"Код платежа: <code>{escape(reference_code)}</code>",
                        "Но очередь выдачи не создалась. Администратор уже видит ошибку, Stars не потеряны.",
                    ]
                )
            )
            return

    await message.answer(
        "\n".join(
            [
                "<b>Оплата Stars подтверждена</b>",
                f"Код платежа: <code>{escape(reference_code)}</code>",
                "Доступ активируется автоматически. Если 3x-ui временно недоступна, worker повторит выдачу.",
                "Ссылку можно будет открыть через /my.",
            ]
        )
    )


@router.callback_query(InvoiceAction.filter(F.action == "paid"))
async def invoice_paid(callback: CallbackQuery, callback_data: InvoiceAction, app_context: AppContext) -> None:
    async with app_context.session_factory() as session:
        invoice = await session.scalar(select(Invoice).where(Invoice.id == callback_data.invoice_id))
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
        if invoice.status == InvoiceStatus.paid_pending_provision.value:
            await callback.answer("Оплата уже подтверждена. Доступ активируется автоматически.")
            return
        if invoice.status == InvoiceStatus.provision_failed.value:
            await callback.answer(
                "Оплата уже подтверждена, но автоматическая выдача пока не удалась. Администратор уже видит ошибку.",
                show_alert=True,
            )
            return
        if invoice.status == InvoiceStatus.paid.value:
            await callback.answer("Оплата по этому инвойсу уже подтверждена.")
            return
        if invoice.status == InvoiceStatus.rejected.value:
            await callback.answer("Этот инвойс уже отклонён.", show_alert=True)
            return
        if invoice.status == InvoiceStatus.expired.value:
            await callback.answer("Срок инвойса уже закончился.", show_alert=True)
            return
        if ensure_utc(invoice.expires_at) <= utc_now():
            expire_open_invoice(invoice)
            await session.commit()
            await callback.answer("Срок инвойса уже закончился.", show_alert=True)
            return
        mark_invoice_pending_review(invoice)
        if invoice.status != InvoiceStatus.pending_review.value:
            await callback.answer("Инвойс нельзя отправить на проверку.", show_alert=True)
            return
        await session.commit()
        admin_text = format_invoice_for_admin(invoice, user)

    for admin_id in app_context.settings.app.admin_ids:
        try:
            await callback.bot.send_message(
                admin_id,
                admin_text,
                reply_markup=admin_invoice_keyboard(invoice.id),
            )
        except TelegramAPIError:
            logging.exception("Failed to notify admin %s about invoice %s", admin_id, invoice.id)

    await callback.answer("Передал платёж админу на проверку.")
    await callback.message.answer("Платёж отправлен на проверку. Как только подтвержу перевод, пришлю доступ.")


@router.message(Command("my"))
@router.message(F.text == "Моя подписка")
async def my_subscription(message: Message, app_context: AppContext) -> None:
    async with app_context.session_factory() as session:
        user = await ensure_user(session, message.from_user, app_context.settings.app.admin_ids)
        subscriptions = await get_user_active_subscriptions(session, user.id)
        open_invoices = await get_open_invoices_for_user(session, user.id)
        await session.commit()

    if subscriptions:
        await message.answer(format_user_subscriptions(subscriptions, app_context.settings.app.field_encryption_key))
        return

    if open_invoices:
        invoice = open_invoices[0]
        if invoice.status == InvoiceStatus.paid_pending_provision.value:
            await message.answer("Оплата подтверждена, доступ активируется. Обычно это занимает меньше минуты.")
            return
        if invoice.status == InvoiceStatus.provision_failed.value:
            await message.answer(
                "Оплата подтверждена, но автоматическая выдача пока не удалась. Администратор уже видит эту ошибку."
            )
            return
        await message.answer(
            (f"У вас есть незавершённый инвойс:\n{format_invoice_for_user(invoice, app_context.settings.payment)}"),
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
                "Доступна оплата переводом на карту/СБП и Telegram Stars.",
                "После оплаты бот отдаст ссылку формата VLESS + REALITY.",
            ]
        )
    )


def _format_plan_payment_choice(plan: PlanDefinition) -> str:
    lines = [f"<b>{escape(plan.title)}</b>"]
    if plan.description:
        lines.extend(["", escape(plan.description)])
    if not plan.provision_access:
        lines.extend(["", "Этот пункт только проверяет оплату Stars и не выдаёт VPN-доступ."])
    lines.extend(["", "Выберите способ оплаты:"])
    return "\n".join(lines)


async def _notify_admins_about_stars_schedule_error(
    message: Message,
    app_context: AppContext,
    reference_code: str,
    exc: Exception,
) -> None:
    for admin_id in app_context.settings.app.admin_ids:
        try:
            await message.bot.send_message(
                admin_id,
                "\n".join(
                    [
                        "<b>Ошибка постановки Stars-платежа в очередь</b>",
                        f"Код платежа: <code>{escape(reference_code)}</code>",
                        f"Пользователь: <code>{message.from_user.id if message.from_user else '-'}</code>",
                        f"Ошибка: <code>{escape(str(exc))}</code>",
                    ]
                ),
            )
        except TelegramAPIError:
            logging.exception("Failed to notify admin %s about Stars schedule error", admin_id)
