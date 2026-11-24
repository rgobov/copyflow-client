from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from copyflow_client.config import SubConfig
from copyflow_client.models import Base, Fill, Order, OrderStatus, RiskDay
from copyflow_core.executors.base import ExecutionAdapter, ExecutionError, Position

log = logging.getLogger(__name__)


async def ensure_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def today_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def ensure_day(session: AsyncSession, sub_id: int, equity_now: float) -> RiskDay:
    """New day => paused auto-resumes (fresh daily budget). Race-safe: the
    monitor and the receiver may both touch the row in the same event loop."""
    day = today_str()
    row = (
        await session.execute(
            select(RiskDay).where(RiskDay.subscription_id == sub_id, RiskDay.day == day)
        )
    ).scalar_one_or_none()
    if row is None:
        row = RiskDay(subscription_id=sub_id, day=day,
                      start_equity=equity_now, peak_equity=equity_now, paused=False)
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            row = (
                await session.execute(
                    select(RiskDay).where(RiskDay.subscription_id == sub_id, RiskDay.day == day)
                )
            ).scalar_one()
            row.peak_equity = max(row.peak_equity or 0.0, equity_now)
    else:
        row.peak_equity = max(row.peak_equity or 0.0, equity_now)
    return row


async def all_time_peak(session: AsyncSession, sub_id: int, current: float) -> float:
    rows = (await session.execute(select(RiskDay).where(RiskDay.subscription_id == sub_id))).scalars().all()
    return max([r.peak_equity for r in rows] + [current])


@dataclass
class ClientRisk:
    day_pnl_pct: float
    drawdown_pct: float
    daily_breach: bool
    drawdown_breach: bool
    paused: bool
    paused_reason: str | None

    @property
    def blocked(self) -> bool:
        return self.paused or self.daily_breach or self.drawdown_breach

    @property
    def reason(self) -> str:
        if self.paused:
            return self.paused_reason or "paused"
        if self.daily_breach:
            return f"daily loss {self.day_pnl_pct:.1f}%"
        return f"drawdown {self.drawdown_pct:.1f}%"


async def assess_risk(session: AsyncSession, cfg: SubConfig, equity_now: float) -> tuple[RiskDay, ClientRisk]:
    day = await ensure_day(session, cfg.subscription_id, equity_now)
    peak = await all_time_peak(session, cfg.subscription_id, equity_now)
    day_pnl = (equity_now - day.start_equity) / day.start_equity * 100 if day.start_equity > 0 else 0.0
    dd = (peak - equity_now) / peak * 100 if peak > 0 else 0.0
    risk = ClientRisk(
        day_pnl_pct=day_pnl,
        drawdown_pct=dd,
        daily_breach=day_pnl <= -cfg.daily_loss_limit_pct,
        drawdown_breach=cfg.max_drawdown_pct > 0 and dd >= cfg.max_drawdown_pct,
        paused=day.paused,
        paused_reason=day.pause_reason,
    )
    return day, risk


async def pause(session: AsyncSession, day: RiskDay, reason: str) -> None:
    day.paused = True
    day.pause_reason = reason[:250]


async def resume(session: AsyncSession, cfg: SubConfig) -> bool:
    day = (
        await session.execute(
            select(RiskDay).where(
                RiskDay.subscription_id == cfg.subscription_id, RiskDay.day == today_str()
            )
        )
    ).scalar_one_or_none()
    if day and day.paused:
        day.paused = False
        day.pause_reason = None
        return True
    return False


async def flatten_all(session: AsyncSession, cfg: SubConfig, adapter: ExecutionAdapter,
                      reason: str) -> int:
    closed = 0
    for pos in await adapter.positions():
        coid = f"clf{int(datetime.now(UTC).timestamp() * 1000)}{pos.symbol[:6]}"[:64]
        try:
            fill = await adapter.close_position(pos.symbol, coid)
        except ExecutionError as e:
            log.error("client flatten failed %s: %s", pos.symbol, e)
            continue
        if fill is None:
            continue
        order = Order(
            client_order_id=coid, venue=adapter.venue, symbol=pos.symbol,
            side=fill.side, kind="close", notional_usd=fill.qty * fill.price,
            status=OrderStatus.FILLED, reason=f"client flatten: {reason}",
        )
        session.add(order)
        await session.flush()
        session.add(Fill(order_id=order.id, symbol=fill.symbol, side=fill.side,
                         qty=fill.qty, price=fill.price, fee_usd=fill.fee_usd))
        closed += 1
        log.warning("client flattened %s %s qty=%.6f (%s)", pos.symbol, fill.side, fill.qty, reason)
    return closed


def resolve_current(positions: list[Position], symbol: str) -> Position | None:
    base = symbol.split("-")[0].upper()
    return next((p for p in positions if p.symbol == symbol or p.symbol.upper() == base), None)
