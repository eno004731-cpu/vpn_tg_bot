from datetime import timedelta
from decimal import Decimal

from vpn_bot.config import PaymentSettings
from vpn_bot.models import Invoice
from vpn_bot.services.payments import format_invoice_for_user
from vpn_bot.utils import utc_now


def test_invoice_formatting_shows_full_card_and_phone() -> None:
    invoice = Invoice(
        id=1,
        user_id=1,
        plan_code="starter",
        plan_title="30 дней / 200 ГБ",
        duration_days=30,
        traffic_limit_bytes=200,
        amount_rub=Decimal("299.12"),
        amount_kopecks=29912,
        reference_code="VPN-000001",
        status="awaiting_transfer",
        expires_at=utc_now() + timedelta(hours=12),
    )
    payment_settings = PaymentSettings(
        bank_name="Demo Bank",
        receiver_name="Demo User",
        card_number="0000000000000000",
        phone="+10000000000",
        invoice_lifetime_hours=12,
        instruction_hint="Переведите точную сумму.",
    )

    text = format_invoice_for_user(invoice, payment_settings)

    assert "СБП по телефону" in text
    assert "+10000000000" in text
    assert "0000 0000 0000 0000" in text
    assert "VPN-000001" in text
