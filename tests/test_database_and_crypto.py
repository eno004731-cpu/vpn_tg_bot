from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from vpn_bot.database import build_session_factory, init_db, normalize_database_url
from vpn_bot.migrations import migrate_sqlite_to_postgres
from vpn_bot.models import Invoice, InvoiceStatus, Subscription, SubscriptionStatus, User
from vpn_bot.services.crypto import decrypt_value, encrypt_value
from vpn_bot.utils import utc_now


def test_normalize_database_url_uses_asyncpg() -> None:
    assert normalize_database_url("postgresql://user:pass@db/app") == "postgresql+asyncpg://user:pass@db/app"
    assert normalize_database_url("postgres://user:pass@db/app") == "postgresql+asyncpg://user:pass@db/app"
    assert normalize_database_url("sqlite+aiosqlite:///tmp.db") == "sqlite+aiosqlite:///tmp.db"


def test_encryption_round_trip_and_plaintext_compatibility() -> None:
    key = "test-field-encryption-key"
    encrypted = encrypt_value("vless://example", key)

    assert encrypted is not None
    assert encrypted.startswith("enc:v1:")
    assert decrypt_value(encrypted, key) == "vless://example"
    assert decrypt_value("plain-value", key) == "plain-value"
    assert encrypt_value(encrypted, key) == encrypted


def test_encrypted_subscription_identifiers_use_unbounded_columns() -> None:
    assert str(Subscription.__table__.c.xui_client_id.type) == "TEXT"
    assert str(Subscription.__table__.c.xui_email.type) == "TEXT"


async def test_migrate_sqlite_to_database_url_preserves_rows(tmp_path) -> None:
    source_path = tmp_path / "source.sqlite3"
    target_path = tmp_path / "target.sqlite3"
    source_engine, source_factory = build_session_factory(source_path)
    await init_db(source_engine)
    async with source_factory() as session:
        user = User(tg_id=123, username="user", full_name="User")
        session.add(user)
        await session.flush()
        invoice = Invoice(
            user_id=user.id,
            plan_code="starter",
            plan_title="Starter",
            duration_days=30,
            traffic_limit_bytes=1024,
            amount_rub=Decimal("100.00"),
            amount_kopecks=10000,
            reference_code="VPN-000001",
            status=InvoiceStatus.paid.value,
            expires_at=utc_now() + timedelta(hours=12),
            paid_at=utc_now(),
        )
        session.add(invoice)
        await session.flush()
        session.add(
            Subscription(
                user_id=user.id,
                source_invoice_id=invoice.id,
                plan_code="starter",
                plan_title="Starter",
                status=SubscriptionStatus.active.value,
                node_code="main",
                xui_client_id="client-id",
                xui_email="tg123@vpn.local",
                access_url="vless://example",
                traffic_limit_bytes=1024,
                started_at=utc_now(),
                ends_at=utc_now() + timedelta(days=30),
            )
        )
        await session.commit()
    await source_engine.dispose()

    summary = await migrate_sqlite_to_postgres(source_path, f"sqlite+aiosqlite:///{target_path}")

    assert summary.users == 1
    assert summary.invoices == 1
    assert summary.subscriptions == 1
