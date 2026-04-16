from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from vpn_bot.config import AppSettings, PaymentSettings, Settings, TrafficPolicySettings, XUISettings
from vpn_bot.models import Subscription
from vpn_bot.services.subscriptions import _apply_daily_traffic_policy
from vpn_bot.services.xui import TrafficSnapshot


class FakePanel:
    def __init__(self) -> None:
        self.calls = []

    async def update_client_speed_limit(
        self,
        inbound_id: int,
        *,
        client_id: str,
        speed_limit_kbytes_per_second: int,
    ) -> None:
        self.calls.append((inbound_id, client_id, speed_limit_kbytes_per_second))


def make_settings() -> Settings:
    return Settings(
        app=AppSettings(
            bot_token="token",
            admin_ids=(1,),
            database_path=Path("data/bot.sqlite3"),
        ),
        payment=PaymentSettings(
            bank_name="Demo",
            receiver_name="Demo",
            card_number="0000000000000000",
            phone=None,
            invoice_lifetime_hours=12,
        ),
        traffic_policy=TrafficPolicySettings(
            enabled=True,
            daily_limit_gb=75,
            throttled_speed_kbytes_per_second=1250,
            timezone="Europe/Moscow",
        ),
        xui=XUISettings(
            base_url="https://panel.example.com/secret",
            username="admin",
            password="secret",
            inbound_id=7,
            public_host="vpn.example.com",
            public_port=443,
        ),
        secrets_file=Path("secrets/runtime.toml"),
        plans_file=Path("config/plans.toml"),
    )


def make_subscription(**overrides) -> Subscription:
    values = {
        "id": 1,
        "user_id": 1,
        "source_invoice_id": 1,
        "plan_code": "starter",
        "plan_title": "30 дней / 310 ГБ",
        "status": "active",
        "xui_client_id": "uuid-1",
        "xui_email": "tg1@vpn.local",
        "access_url": "vless://example",
        "traffic_limit_bytes": 310 * 1024 * 1024 * 1024,
        "upload_bytes": 0,
        "download_bytes": 0,
        "traffic_used_bytes": 0,
        "daily_traffic_date": datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(),
        "daily_baseline_bytes": 0,
        "speed_limit_kbytes_per_second": 0,
        "started_at": datetime.now(timezone.utc),
        "ends_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return Subscription(**values)


async def test_daily_policy_throttles_after_75_gb() -> None:
    settings = make_settings()
    subscription = make_subscription()
    snapshot = TrafficSnapshot(
        email=subscription.xui_email,
        upload_bytes=0,
        download_bytes=settings.traffic_policy.daily_limit_bytes,
        total_bytes=settings.traffic_policy.daily_limit_bytes,
    )
    panel = FakePanel()

    changed = await _apply_daily_traffic_policy(subscription, snapshot, panel, settings)

    assert changed
    assert subscription.speed_limit_kbytes_per_second == 1250
    assert panel.calls == [(7, "uuid-1", 1250)]


async def test_daily_policy_resets_speed_on_new_day() -> None:
    settings = make_settings()
    subscription = make_subscription(
        daily_traffic_date="2000-01-01",
        daily_baseline_bytes=0,
        speed_limit_kbytes_per_second=1250,
    )
    snapshot = TrafficSnapshot(
        email=subscription.xui_email,
        upload_bytes=0,
        download_bytes=100 * 1024 * 1024 * 1024,
        total_bytes=100 * 1024 * 1024 * 1024,
    )
    panel = FakePanel()

    changed = await _apply_daily_traffic_policy(subscription, snapshot, panel, settings)

    assert changed
    assert subscription.daily_baseline_bytes == snapshot.total_bytes
    assert subscription.speed_limit_kbytes_per_second == 0
    assert panel.calls == [(7, "uuid-1", 0)]


async def test_daily_policy_uses_plan_limit_override() -> None:
    settings = make_settings()
    subscription = make_subscription()
    snapshot = TrafficSnapshot(
        email=subscription.xui_email,
        upload_bytes=0,
        download_bytes=100 * 1024 * 1024 * 1024,
        total_bytes=100 * 1024 * 1024 * 1024,
    )
    panel = FakePanel()

    changed = await _apply_daily_traffic_policy(
        subscription,
        snapshot,
        panel,
        settings,
        plan_daily_limit_bytes=150 * 1024 * 1024 * 1024,
    )

    assert changed is False
    assert subscription.speed_limit_kbytes_per_second == 0
    assert panel.calls == []
