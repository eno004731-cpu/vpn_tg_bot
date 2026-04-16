from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class InvoiceStatus(str, Enum):
    awaiting_transfer = "awaiting_transfer"
    pending_review = "pending_review"
    paid = "paid"
    rejected = "rejected"
    expired = "expired"


class SubscriptionStatus(str, Enum):
    active = "active"
    expired = "expired"
    revoked = "revoked"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="user")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_code: Mapped[str] = mapped_column(String(64))
    plan_title: Mapped[str] = mapped_column(String(255))
    duration_days: Mapped[int] = mapped_column(Integer)
    traffic_limit_bytes: Mapped[int] = mapped_column(BigInteger)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    amount_kopecks: Mapped[int] = mapped_column(Integer, index=True)
    reference_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default=InvoiceStatus.awaiting_transfer.value)
    admin_note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="invoices")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="source_invoice")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_invoice_id: Mapped[Optional[int]] = mapped_column(ForeignKey("invoices.id"))
    plan_code: Mapped[str] = mapped_column(String(64))
    plan_title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=SubscriptionStatus.active.value)
    xui_client_id: Mapped[str] = mapped_column(String(64), unique=True)
    xui_email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    access_url: Mapped[str] = mapped_column(Text)
    traffic_limit_bytes: Mapped[int] = mapped_column(BigInteger)
    upload_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    download_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    source_invoice: Mapped[Optional["Invoice"]] = relationship(back_populates="subscriptions")
