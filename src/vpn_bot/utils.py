from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Normalize naive or timezone-aware datetimes to timezone-aware UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_bytes(value: int) -> str:
    """Render a byte count as a compact human-readable string."""

    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.2f} {unit}"
        number /= 1024
    return f"{value} B"


def decimal_to_kopecks(value: Decimal) -> int:
    """Convert a Decimal ruble amount to integer kopecks."""

    normalized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(normalized * 100)


def kopecks_to_decimal(value: int) -> Decimal:
    """Convert integer kopecks to a two-decimal ruble amount."""

    return (Decimal(value) / Decimal(100)).quantize(Decimal("0.01"))


def format_card_number(card_number: str) -> str:
    """Group card digits by fours for payment instructions."""

    clean = "".join(ch for ch in card_number if ch.isdigit())
    if len(clean) < 12:
        return card_number
    return " ".join(clean[index : index + 4] for index in range(0, len(clean), 4))


def mask_card_number(card_number: str) -> str:
    """Mask a card number while keeping enough digits for admin diagnostics."""

    clean = "".join(ch for ch in card_number if ch.isdigit())
    if len(clean) < 8:
        return card_number
    return f"{clean[:4]} {clean[4:8]} **** {clean[-4:]}"
