from __future__ import annotations

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
