from decimal import Decimal

from vpn_bot.services.payments import reserve_unique_amount
from vpn_bot.utils import decimal_to_kopecks


def test_reserve_unique_amount_skips_existing_values() -> None:
    base = Decimal("299.00")
    used = {decimal_to_kopecks(Decimal("299.11")), decimal_to_kopecks(Decimal("299.12"))}

    candidate = reserve_unique_amount(base, used, seed=0)

    assert candidate == Decimal("299.13")
