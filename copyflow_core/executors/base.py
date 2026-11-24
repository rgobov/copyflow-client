from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class Fill:
    symbol: str
    side: str  # buy | sell
    qty: float  # base units
    price: float
    fee_usd: float = 0.0


@dataclass
class Position:
    symbol: str
    side: str  # long | short
    qty: float
    entry_price: float


@dataclass
class AccountState:
    equity_usd: float
    positions: list[Position]


class ExecutionError(Exception):
    pass


class ExecutionAdapter(abc.ABC):
    """Common execution interface for paper / hyperliquid / binance."""

    venue: str = "abstract"

    @abc.abstractmethod
    async def market_order(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        client_order_id: str,
        reduce_only: bool = False,
        ref_price: float | None = None,
        slippage_bps: int = 0,
    ) -> Fill:
        """Place a market order by notional USD value. Idempotent by client_order_id."""

    @abc.abstractmethod
    async def positions(self) -> list[Position]:
        ...

    @abc.abstractmethod
    async def account_state(self) -> AccountState:
        ...

    @abc.abstractmethod
    async def close_position(self, symbol: str, client_order_id: str) -> Fill | None:
        ...

    @abc.abstractmethod
    async def mark_price(self, symbol: str) -> float | None:
        """Latest public mark price for guards and paper fills."""
