from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import insert, select, text

from vpn_bot.database import build_session_factory, init_db
from vpn_bot.models import Invoice, Job, OneTimePlanPurchase, OneTimePlanReservation, Subscription, User


@dataclass(frozen=True)
class MigrationSummary:
    """Row counts copied by the SQLite-to-Postgres migration."""

    users: int
    invoices: int
    one_time_plan_purchases: int
    one_time_plan_reservations: int
    subscriptions: int
    jobs: int


async def migrate_sqlite_to_postgres(sqlite_path: Path, database_url: str) -> MigrationSummary:
    """Copy existing SQLite rows into an empty PostgreSQL database."""

    source_engine, source_factory = build_session_factory(sqlite_path)
    target_engine, target_factory = build_session_factory(database_url=database_url)
    await init_db(source_engine)
    await init_db(target_engine)

    counts: dict[str, int] = {}
    try:
        async with source_factory() as source, target_factory() as target:
            for model, key in (
                (User, "users"),
                (Invoice, "invoices"),
                (OneTimePlanPurchase, "one_time_plan_purchases"),
                (OneTimePlanReservation, "one_time_plan_reservations"),
                (Subscription, "subscriptions"),
                (Job, "jobs"),
            ):
                rows = list(await source.scalars(select(model).order_by(model.id.asc())))
                counts[key] = len(rows)
                if not rows:
                    continue
                for row in rows:
                    values = {column.name: getattr(row, column.name) for column in model.__table__.columns}
                    await target.execute(insert(model).values(**values))
            await target.commit()
            await _reset_postgres_sequences(target)
            await target.commit()
    finally:
        await source_engine.dispose()
        await target_engine.dispose()

    return MigrationSummary(
        users=counts.get("users", 0),
        invoices=counts.get("invoices", 0),
        one_time_plan_purchases=counts.get("one_time_plan_purchases", 0),
        one_time_plan_reservations=counts.get("one_time_plan_reservations", 0),
        subscriptions=counts.get("subscriptions", 0),
        jobs=counts.get("jobs", 0),
    )


async def _reset_postgres_sequences(session) -> None:
    """Move Postgres serial sequences past imported primary keys."""

    if session.bind is None or session.bind.url.get_backend_name() != "postgresql":
        return
    for table_name in (
        "users",
        "invoices",
        "one_time_plan_purchases",
        "one_time_plan_reservations",
        "subscriptions",
        "jobs",
    ):
        await session.execute(
            text(
                f"""
                SELECT setval(
                    pg_get_serial_sequence('{table_name}', 'id'),
                    COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                    (SELECT MAX(id) IS NOT NULL FROM {table_name})
                )
                """
            )
        )
