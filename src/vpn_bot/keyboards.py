from __future__ import annotations

from typing import Optional

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from vpn_bot.config import PlanDefinition
from vpn_bot.services.custom_plans import (
    CUSTOM_PLAN_DAY_PRESETS,
    CUSTOM_PLAN_DEFAULT_DAYS,
    CUSTOM_PLAN_DEFAULT_DEVICES,
    CUSTOM_PLAN_KIND,
    PREMIUM_PLAN_KIND,
    build_custom_plan,
    clamp_custom_days,
    clamp_custom_devices,
)


class PlanChoice(CallbackData, prefix="plan"):
    code: str


class PaymentMethodChoice(CallbackData, prefix="pay_method"):
    code: str
    method: str


class CustomPlanAction(CallbackData, prefix="cp"):
    kind: str
    days: int
    devices: int
    action: str


class UserNavigationAction(CallbackData, prefix="user_nav"):
    action: str


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
                text="Собрать Custom",
                callback_data=CustomPlanAction(
                    kind=CUSTOM_PLAN_KIND,
                    days=CUSTOM_PLAN_DEFAULT_DAYS,
                    devices=CUSTOM_PLAN_DEFAULT_DEVICES,
                    action="show",
                ).pack(),
            )
        ],
        [
            InlineKeyboardButton(
                text="Собрать Custom Premium",
                callback_data=CustomPlanAction(
                    kind=PREMIUM_PLAN_KIND,
                    days=CUSTOM_PLAN_DEFAULT_DAYS,
                    devices=CUSTOM_PLAN_DEFAULT_DEVICES,
                    action="show",
                ).pack(),
            )
        ],
    ]
    rows.extend(
        [
            InlineKeyboardButton(
                text=f"{plan.title} - {plan_price_label(plan)}",
                callback_data=PlanChoice(code=plan.code).pack(),
            )
        ]
        for plan in plans
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def custom_plan_builder_keyboard(kind: str, days: int, devices: int) -> InlineKeyboardMarkup:
    days = clamp_custom_days(days)
    devices = clamp_custom_devices(devices)
    preset_rows = [
        CUSTOM_PLAN_DAY_PRESETS[0:4],
        CUSTOM_PLAN_DAY_PRESETS[4:8],
        CUSTOM_PLAN_DAY_PRESETS[8:11],
        CUSTOM_PLAN_DAY_PRESETS[11:13],
    ]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{preset} дн",
                callback_data=CustomPlanAction(kind=kind, days=days, devices=devices, action=f"p{preset}").pack(),
            )
            for preset in preset_row
        ]
        for preset_row in preset_rows
    ]
    rows.extend(
        [
            [
                _custom_plan_button(kind, days, devices, "-30 дн", "dm30"),
                _custom_plan_button(kind, days, devices, "-7 дн", "dm7"),
                _custom_plan_button(kind, days, devices, "-1 дн", "dm1"),
            ],
            [
                _custom_plan_button(kind, days, devices, "+1 дн", "dp1"),
                _custom_plan_button(kind, days, devices, "+7 дн", "dp7"),
                _custom_plan_button(kind, days, devices, "+30 дн", "dp30"),
            ],
            [
                _custom_plan_button(kind, days, devices, "-1 устройство", "um1"),
                _custom_plan_button(kind, days, devices, "+1 устройство", "up1"),
            ],
            [
                InlineKeyboardButton(
                    text="Выбрать этот тариф",
                    callback_data=CustomPlanAction(kind=kind, days=days, devices=devices, action="pay").pack(),
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_custom_plan_builder(kind: str, days: int, devices: int) -> str:
    plan = build_custom_plan(kind, days, devices)
    lines = [
        f"<b>{plan.title}</b>",
        "",
        f"Дней: <code>{plan.duration_days}</code>",
        f"Устройств: <code>{plan.device_limit}</code>",
        f"Стоимость: <code>{plan.price_rub}</code> ₽ / <code>{plan.price_stars}</code> ⭐",
    ]
    if plan.traffic_limit_gb > 0:
        lines.append(f"Трафик: <code>{plan.traffic_limit_gb}</code> ГБ")
    else:
        lines.append("Трафик: <code>Безлимит</code>")
    lines.extend(["", "Настройте дни и устройства:"])
    return "\n".join(lines)


def _custom_plan_button(kind: str, days: int, devices: int, text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=CustomPlanAction(kind=kind, days=days, devices=devices, action=action).pack(),
    )


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
    rows.append(
        [
            InlineKeyboardButton(
                text="Назад к тарифам",
                callback_data=UserNavigationAction(action="plans").pack(),
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
