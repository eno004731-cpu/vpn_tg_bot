from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from vpn_bot.config import PlanDefinition


class PlanChoice(CallbackData, prefix="plan"):
    code: str


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
    rows = [
        [
            InlineKeyboardButton(
                text=f"{plan.title} - {plan.price_rub} ₽",
                callback_data=PlanChoice(code=plan.code).pack(),
            )
        ]
        for plan in plans
    ]
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

