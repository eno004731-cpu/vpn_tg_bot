from datetime import datetime, timezone

from vpn_bot.utils import ensure_utc


def test_ensure_utc_adds_utc_to_naive_datetime() -> None:
    naive = datetime(2026, 4, 15, 12, 0, 0)

    normalized = ensure_utc(naive)

    assert normalized.tzinfo == timezone.utc
    assert normalized.hour == 12
