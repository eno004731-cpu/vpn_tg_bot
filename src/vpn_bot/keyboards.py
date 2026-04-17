from __future__ import annotations

from typing import Optional

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from vpn_bot.config import PlanDefinition


class PlanChoice(CallbackData, prefix="plan"):
    code: str


class PaymentMethodChoice(CallbackData, prefix="pay_method"):
    code: str
    method: str


class InvoiceAction(CallbackData, prefix="invoice"):
    action: str
    invoice_id: int


class AdminInvoiceAction(CallbackData, prefix="admin_invoice"):
    action: str
    invoice_id: int


class AdminUsersPage(CallbackData, prefix="aup"):
    page: int


class AdminInvoicesPage(CallbackData, prefix="aip"):
    page: int


class AdminUserAction(CallbackData, prefix="au"):
    action: str
    user_id: int
    page: int


class AdminSubscriptionAction(CallbackData, prefix="asub"):
    action: str
    subscription_id: int
    user_id: int
    page: int


class AdminGrantPlan(CallbackData, prefix="ag"):
    user_id: int
    plan_code: str
    page: int


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Купить подписку"), KeyboardButton(text="Моя подписка")],
            [KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


def plans_keyboard(plans: list[PlanDefinition]) -> InlineKeyboardMarkup:
    def plan_price_label(plan: PlanDefinition) -> str:
        prices = []
        if plan.supports_transfer:
            prices.append(f"{plan.price_rub} ₽")
        if plan.supports_stars:
            prices.append(f"{plan.price_stars} ⭐")
        return " / ".join(prices) if prices else "недоступен"

    rows = [
        [
            InlineKeyboardButton(
                text=f"{plan.title} - {plan_price_label(plan)}",
                callback_data=PlanChoice(code=plan.code).pack(),
            )
        ]
        for plan in plans
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_methods_keyboard(plan: PlanDefinition) -> InlineKeyboardMarkup:
    rows = []
    if plan.supports_transfer:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Перевод на карту / СБП",
                    callback_data=PaymentMethodChoice(code=plan.code, method="transfer").pack(),
                )
            ]
        )
    if plan.supports_stars:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Оплатить Stars: {plan.price_stars} ⭐",
                    callback_data=PaymentMethodChoice(code=plan.code, method="stars").pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_keyboard(invoice_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Я оплатил",
                    callback_data=InvoiceAction(action="paid", invoice_id=invoice_id).pack(),
                )
            ]
        ]
    )


def admin_invoice_keyboard(invoice_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить",
                    callback_data=AdminInvoiceAction(action="approve", invoice_id=invoice_id).pack(),
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=AdminInvoiceAction(action="reject", invoice_id=invoice_id).pack(),
                ),
            ]
        ]
    )


def admin_users_keyboard(
    users: list[tuple[int, str]], page: int, has_prev: bool, has_next: bool
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=label,
                callback_data=AdminUserAction(action="view", user_id=user_id, page=page).pack(),
            )
        ]
        for user_id, label in users
    ]
    navigation = []
    if has_prev:
        navigation.append(
            InlineKeyboardButton(
                text="<<",
                callback_data=AdminUsersPage(page=page - 1).pack(),
            )
        )
    if has_next:
        navigation.append(
            InlineKeyboardButton(
                text=">>",
                callback_data=AdminUsersPage(page=page + 1).pack(),
            )
        )
    if navigation:
        rows.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_invoices_page_keyboard(page: int, has_prev: bool, has_next: bool) -> Optional[InlineKeyboardMarkup]:
    navigation = []
    if has_prev:
        navigation.append(
            InlineKeyboardButton(
                text="<<",
                callback_data=AdminInvoicesPage(page=page - 1).pack(),
            )
        )
    if has_next:
        navigation.append(
            InlineKeyboardButton(
                text=">>",
                callback_data=AdminInvoicesPage(page=page + 1).pack(),
            )
        )
    if not navigation:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[navigation])


def admin_user_keyboard(
    user_id: int,
    page: int,
    active_subscription_ids: list[int],
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="Дать доступ",
                callback_data=AdminUserAction(action="grant", user_id=user_id, page=page).pack(),
            )
        ]
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=f"Забрать #{subscription_id}",
                    callback_data=AdminSubscriptionAction(
                        action="revoke",
                        subscription_id=subscription_id,
                        user_id=user_id,
                        page=page,
                    ).pack(),
                )
            ]
            for subscription_id in active_subscription_ids
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="Назад к списку",
                callback_data=AdminUsersPage(page=page).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_search_back_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад к списку",
                    callback_data=AdminUsersPage(page=page).pack(),
                )
            ]
        ]
    )


def admin_grant_plans_keyboard(user_id: int, page: int, plans: list[PlanDefinition]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=plan.title,
                callback_data=AdminGrantPlan(user_id=user_id, plan_code=plan.code, page=page).pack(),
            )
        ]
        for plan in plans
        if plan.provision_access
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="Назад",
                callback_data=AdminUserAction(action="view", user_id=user_id, page=page).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
