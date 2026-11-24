from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ReceivedSignal(Base):
    """Every accepted webhook, for idempotency and audit."""

    __tablename__ = "received_signals"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subscription_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionStatus(str, enum.Enum):
    AWAITING = "awaiting_confirm"
    EXECUTED = "executed"
    SKIPPED = "skipped"
    DECLINED = "declined"
    EXPIRED = "expired"


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), ForeignKey("received_signals.signal_id"))
    status: Mapped[DecisionStatus] = mapped_column(Enum(DecisionStatus), default=DecisionStatus.AWAITING)
    action: Mapped[str] = mapped_column(String(16), default="")
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    notional_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OrderStatus(str, enum.Enum):
    NEW = "new"
    FILLED = "filled"
    FAILED = "failed"


class Order(Base):
    __tablename__ = "client_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(96), unique=True)
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    venue: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(16), default="open")
    notional_usd: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.NEW)
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Fill(Base):
    __tablename__ = "client_fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("client_orders.id"))
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskDay(Base):
    """Client-side risk bookkeeping per subscription per UTC day."""

    __tablename__ = "risk_days"
    __table_args__ = (UniqueConstraint("subscription_id", "day", name="uq_risk_sub_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(Integer)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    start_equity: Mapped[float] = mapped_column(Float, default=0.0)
    peak_equity: Mapped[float] = mapped_column(Float, default=0.0)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    daily_loss_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    drawdown_notified: Mapped[bool] = mapped_column(Boolean, default=False)
