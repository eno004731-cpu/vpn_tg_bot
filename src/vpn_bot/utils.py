from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.2f} {unit}"
        number /= 1024
    return f"{value} B"


def decimal_to_kopecks(value: Decimal) -> int:
    normalized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(normalized * 100)


def kopecks_to_decimal(value: int) -> Decimal:
    return (Decimal(value) / Decimal(100)).quantize(Decimal("0.01"))


def format_card_number(card_number: str) -> str:
    clean = "".join(ch for ch in card_number if ch.isdigit())
    if len(clean) < 12:
        return card_number
    return " ".join(clean[index : index + 4] for index in range(0, len(clean), 4))


def mask_card_number(card_number: str) -> str:
    clean = "".join(ch for ch in card_number if ch.isdigit())
    if len(clean) < 8:
        return card_number
    return f"{clean[:4]} {clean[4:8]} **** {clean[-4:]}"
