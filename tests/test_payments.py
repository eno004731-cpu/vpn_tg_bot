from decimal import Decimal

from vpn_bot.services.payments import (
    build_stars_payload,
    build_stars_reference,
    parse_stars_payload,
    reserve_unique_amount,
)
from vpn_bot.utils import decimal_to_kopecks


def test_reserve_unique_amount_skips_existing_values() -> None:
    base = Decimal("299.00")
    used = {decimal_to_kopecks(Decimal("299.11")), decimal_to_kopecks(Decimal("299.12"))}

    candidate = reserve_unique_amount(base, used, seed=0)

    assert candidate == Decimal("299.13")


def test_stars_payload_round_trip() -> None:
    payload = build_stars_payload("starter", 123456789)

    parsed = parse_stars_payload(payload)

    assert parsed.plan_code == "starter"
    assert parsed.user_tg_id == 123456789


def test_stars_reference_is_stable_and_short() -> None:
    reference = build_stars_reference("charge-id")

    assert reference == build_stars_reference("charge-id")
    assert reference.startswith("XTR-")
    assert len(reference) <= 64
