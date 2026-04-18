from decimal import Decimal

from vpn_bot.services.custom_plans import (
    CUSTOM_PLAN_KIND,
    PREMIUM_PLAN_KIND,
    build_custom_plan,
    build_custom_plan_code,
    custom_base_traffic_gb,
    custom_price_rub,
    custom_traffic_gb,
    parse_custom_plan_code,
    premium_price_rub,
    resolve_plan,
)


def test_custom_traffic_curve_for_short_periods() -> None:
    assert custom_base_traffic_gb(1) == 25
    assert custom_base_traffic_gb(3) == 60
    assert custom_base_traffic_gb(7) == 126
    assert custom_base_traffic_gb(8) == 144


def test_custom_traffic_is_multiplied_by_devices() -> None:
    assert custom_traffic_gb(30, 1) == 540
    assert custom_traffic_gb(30, 2) == 756
    assert custom_traffic_gb(30, 3) == 1059
    assert custom_traffic_gb(30, 4) == 1482


def test_custom_price_curve_and_device_multiplier() -> None:
    assert custom_price_rub(1, 1) == Decimal("30.00")
    assert custom_price_rub(3, 1) == Decimal("40.00")
    assert custom_price_rub(7, 1) == Decimal("70.00")
    assert custom_price_rub(30, 1) == Decimal("150.00")
    assert custom_price_rub(30, 2) == Decimal("240.00")
    assert custom_price_rub(30, 3) == Decimal("370.00")
    assert custom_price_rub(90, 1) == Decimal("400.00")
    assert custom_price_rub(180, 1) == Decimal("730.00")
    assert custom_price_rub(365, 1) == Decimal("1570.00")


def test_premium_price_is_unlimited_and_uses_device_multiplier() -> None:
    plan = build_custom_plan(PREMIUM_PLAN_KIND, 30, 2)

    assert plan.traffic_limit_gb == 0
    assert plan.traffic_limit_bytes == 0
    assert plan.daily_limit_gb == 0
    assert plan.device_limit == 2
    assert premium_price_rub(30, 1) == Decimal("370.00")
    assert premium_price_rub(30, 2) == Decimal("540.00")
    assert premium_price_rub(30, 3) == Decimal("780.00")


def test_dynamic_plan_code_round_trip_and_resolve() -> None:
    code = build_custom_plan_code(CUSTOM_PLAN_KIND, 90, 3)
    params = parse_custom_plan_code(code)
    plan = resolve_plan({}, code)

    assert code == "custom_v1_d90_u3"
    assert params is not None
    assert params.days == 90
    assert params.devices == 3
    assert plan is not None
    assert plan.code == code
    assert plan.duration_days == 90
    assert plan.device_limit == 3
    assert plan.traffic_limit_gb == custom_traffic_gb(90, 3)
