from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class Side(str, enum.Enum):
    LONG = "long"
    SHORT = "short"


class Action(str, enum.Enum):
    OPEN = "open"
    INCREASE = "increase"
    REDUCE = "reduce"
    FLIP = "flip"
    CLOSE = "close"


class Instrument(BaseModel):
    """Venue-agnostic instrument reference."""

    venue: str = "paper"
    symbol: str = "BTC"

    def key(self) -> str:
        return f"{self.venue}:{self.symbol}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SignalEvent(BaseModel):
    """Canonical signal consumed by the copy engine.

    size_frac: share of the leader equity (0..1+) for open/increase,
    fraction of current position (0..1) for reduce.
    size_abs_usd: absolute notional, overrides size_frac when set.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    leader_id: int
    instrument: Instrument
    action: Action
    side: Side
    size_frac: float | None = None
    size_abs_usd: float | None = None
    ref_price: float | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    leverage: float | None = None
    ts: datetime = Field(default_factory=_utcnow)

    def size_usd(self, leader_equity_usd: float) -> float:
        """Notional the leader trades, in USD."""
        if self.size_abs_usd is not None:
            return self.size_abs_usd
        if self.size_frac is not None:
            return self.size_frac * leader_equity_usd
        return 0.0
