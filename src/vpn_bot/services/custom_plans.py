from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Mapping, Optional

from vpn_bot.config import PlanDefinition

CUSTOM_PLAN_KIND = "custom"
PREMIUM_PLAN_KIND = "premium"
CUSTOM_PLAN_DEFAULT_DAYS = 30
CUSTOM_PLAN_DEFAULT_DEVICES = 1
CUSTOM_PLAN_MIN_DAYS = 1
CUSTOM_PLAN_MAX_DAYS = 365
CUSTOM_PLAN_MIN_DEVICES = 1
CUSTOM_PLAN_MAX_DEVICES = 10
CUSTOM_PLAN_DAY_PRESETS = (1, 3, 7, 14, 21, 30, 45, 60, 90, 120, 180, 270, 365)

_CUSTOM_PLAN_RE = re.compile(r"^(custom|premium)_v1_d(\d+)_u(\d+)$")


@dataclass(frozen=True)
class CustomPlanParams:
    kind: str
    days: int
    devices: int


def clamp_custom_days(days: int) -> int:
    return min(max(int(days), CUSTOM_PLAN_MIN_DAYS), CUSTOM_PLAN_MAX_DAYS)


def clamp_custom_devices(devices: int) -> int:
    return min(max(int(devices), CUSTOM_PLAN_MIN_DEVICES), CUSTOM_PLAN_MAX_DEVICES)


def normalize_custom_kind(kind: str) -> str:
    if kind not in {CUSTOM_PLAN_KIND, PREMIUM_PLAN_KIND}:
        raise ValueError("Неизвестный тип конструктора тарифа.")
    return kind


def build_custom_plan_code(kind: str, days: int, devices: int) -> str:
    kind = normalize_custom_kind(kind)
    return f"{kind}_v1_d{clamp_custom_days(days)}_u{clamp_custom_devices(devices)}"


def parse_custom_plan_code(code: str) -> Optional[CustomPlanParams]:
    match = _CUSTOM_PLAN_RE.match(code)
    if match is None:
        return None
    kind, days_raw, devices_raw = match.groups()
    days = int(days_raw)
    devices = int(devices_raw)
    if days != clamp_custom_days(days) or devices != clamp_custom_devices(devices):
        return None
    return CustomPlanParams(kind=kind, days=days, devices=devices)


def resolve_plan(plans: Mapping[str, PlanDefinition], code: str) -> Optional[PlanDefinition]:
    plan = plans.get(code)
    if plan is not None:
        return plan
    params = parse_custom_plan_code(code)
    if params is None:
        return None
    return build_custom_plan(params.kind, params.days, params.devices)


def build_custom_plan(kind: str, days: int, devices: int) -> PlanDefinition:
    kind = normalize_custom_kind(kind)
    days = clamp_custom_days(days)
    devices = clamp_custom_devices(devices)
    if kind == PREMIUM_PLAN_KIND:
        return _build_premium_plan(days, devices)
    return _build_limited_custom_plan(days, devices)


def custom_base_traffic_gb(days: int) -> int:
    days = clamp_custom_days(days)
    if days < 7:
        return int(round(25 * (days**0.80)))
    if days == 7:
        return 126
    return 126 + (days - 7) * 18


def custom_traffic_gb(days: int, devices: int) -> int:
    days = clamp_custom_days(days)
    devices = clamp_custom_devices(devices)
    return int(math.ceil(custom_base_traffic_gb(days) * (1.4 ** (devices - 1))))


def custom_duration_price(days: int) -> Decimal:
    days = clamp_custom_days(days)
    if days == 1:
        return Decimal("25")
    if days <= 3:
        return Decimal("40")
    if days <= 7:
        return Decimal("40") + Decimal(str((days - 3) * 7.5))
    if days <= 30:
        return Decimal("70") + Decimal(str((days - 7) * (80 / 23)))
    if days <= 180:
        return Decimal(str(150 * ((days / 30) ** 0.88)))

    price_180 = 150 * ((180 / 30) ** 0.88)
    return Decimal(str(price_180 + (days - 180) * (price_180 / 180) * 1.12))


def custom_price_rub(days: int, devices: int) -> Decimal:
    devices = clamp_custom_devices(devices)
    price = custom_duration_price(days) * Decimal(str(1.55 ** (devices - 1)))
    return _ceil_to_10(price)


def premium_price_rub(days: int, devices: int) -> Decimal:
    days = clamp_custom_days(days)
    devices = clamp_custom_devices(devices)
    if days == 1:
        duration_price = Decimal("90")
    elif days <= 3:
        duration_price = Decimal("110")
    else:
        duration_price = custom_duration_price(days) * Decimal("2.45")
    return _ceil_to_10(duration_price * Decimal(str(1.45 ** (devices - 1))))


def _build_limited_custom_plan(days: int, devices: int) -> PlanDefinition:
    traffic_gb = custom_traffic_gb(days, devices)
    price = custom_price_rub(days, devices)
    return PlanDefinition(
        code=build_custom_plan_code(CUSTOM_PLAN_KIND, days, devices),
        title=f"Custom: {days} дней / {devices} устройств / {traffic_gb} ГБ",
        price_rub=price,
        price_stars=int(price),
        duration_days=days,
        traffic_limit_gb=traffic_gb,
        description=(f"Гибкая подписка: {traffic_gb} ГБ на {days} дней. Можно подключить устройств: {devices}."),
        device_limit=devices,
    )


def _build_premium_plan(days: int, devices: int) -> PlanDefinition:
    price = premium_price_rub(days, devices)
    return PlanDefinition(
        code=build_custom_plan_code(PREMIUM_PLAN_KIND, days, devices),
        title=f"Custom Premium: {days} дней / {devices} устройств / Безлимит",
        price_rub=price,
        price_stars=int(price),
        duration_days=days,
        traffic_limit_gb=0,
        description=f"Безлимитный трафик на {days} дней. Можно подключить устройств: {devices}.",
        daily_limit_gb=0,
        device_limit=devices,
    )


def _ceil_to_10(value: Decimal) -> Decimal:
    rounded = (value / Decimal("10")).to_integral_value(rounding=ROUND_CEILING) * Decimal("10")
    return rounded.quantize(Decimal("0.01"))
