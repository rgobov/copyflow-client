from __future__ import annotations

import asyncio
from dataclasses import dataclass

from copyflow_core.executors.base import AccountState, ExecutionAdapter, ExecutionError, Fill, Position


@dataclass
class PaperConfig:
    starting_equity_usd: float = 1_000.0
    taker_fee_bps: float = 3.5
    slippage_bps: float = 0.0  # extra on top of slippage set per-subscription


@dataclass
class _PaperPosition:
    side: str
    qty: float
    entry_price: float


class PaperExecutor(ExecutionAdapter):
    """Virtual venue: fills at mark price + slippage, keeps virtual equity.

    Prices come from a price provider (e.g. Binance/HL public ticker); if the
    provider has no price, falls back to ref_price passed with the order.
    """

    venue = "paper"

    def __init__(self, config: PaperConfig, price_provider=None):
        self._cfg = config
        self._price_provider = price_provider
        self._equity = config.starting_equity_usd
        self._positions: dict[str, _PaperPosition] = {}
        self._seen_order_ids: set[str] = set()
        self._lock = asyncio.Lock()

    # ---- pricing ----
    async def _fill_price(self, symbol: str, side: str, ref_price: float | None, slippage_bps: int) -> float:
        price = None
        if self._price_provider is not None:
            price = await self._price_provider(symbol)
        if not price:
            price = ref_price
        if not price:
            raise ExecutionError(f"paper: no price for {symbol}")
        slip = (slippage_bps + self._cfg.slippage_bps) / 10_000.0
        if side == "buy":
            return price * (1 + slip)
        return price * (1 - slip)

    # ---- trading ----
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
        if client_order_id in self._seen_order_ids:
            raise ExecutionError(f"paper: duplicate client_order_id {client_order_id}")
        async with self._lock:
            self._seen_order_ids.add(client_order_id)

        price = await self._fill_price(symbol, side, ref_price, slippage_bps)
        qty = notional_usd / price

        # reduce-only can never flip a position: clamp to what is held
        if reduce_only:
            async with self._lock:
                pos = self._positions.get(symbol)
            if pos is None:
                raise ExecutionError(f"paper: nothing to close for {symbol}")
            if (pos.side == "long") == (side == "sell"):
                qty = min(qty, pos.qty)
            if qty <= 0:
                raise ExecutionError(f"paper: nothing to close for {symbol}")

        fee = notional_usd * self._cfg.taker_fee_bps / 10_000.0

        async with self._lock:
            self._apply_fill(symbol, side, qty, price, fee)
        return Fill(symbol=symbol, side=side, qty=qty, price=price, fee_usd=fee)

    def _apply_fill(self, symbol: str, side: str, qty: float, price: float, fee: float) -> None:
        pos = self._positions.get(symbol)
        signed = qty if side == "buy" else -qty

        if pos is None:
            if qty <= 0:
                return
            self._positions[symbol] = _PaperPosition(
                side="long" if side == "buy" else "short", qty=qty, entry_price=price
            )
            self._equity -= fee
            return

        pos_signed = pos.qty if pos.side == "long" else -pos.qty
        closing = (signed > 0) != (pos_signed > 0)
        close_qty = min(qty, abs(pos_signed)) if closing else 0.0

        if closing:
            pnl = (price - pos.entry_price) * close_qty * (1 if pos.side == "long" else -1)
            self._equity += pnl - fee
            remaining = abs(pos_signed) - close_qty
            if remaining <= 1e-12:
                del self._positions[symbol]
                opened_qty = qty - close_qty
                # slippage can leave a dust remainder on a full close; treat < $0.01 as flat
                if opened_qty * price > 0.01:  # flip
                    self._positions[symbol] = _PaperPosition(
                        side="long" if side == "buy" else "short",
                        qty=opened_qty,
                        entry_price=price,
                    )
            else:
                pos.qty = remaining
        else:
            if pos.qty <= 1e-12:
                pos.entry_price = price
            total = pos.qty + qty
            pos.entry_price = (pos.entry_price * pos.qty + price * qty) / total
            pos.qty = total
            self._equity -= fee

    async def close_position(self, symbol: str, client_order_id: str) -> Fill | None:
        async with self._lock:
            pos = self._positions.get(symbol)
        if pos is None:
            return None
        side = "sell" if pos.side == "long" else "buy"
        notional = pos.qty * (await self.mark_price(symbol) or pos.entry_price)
        return await self.market_order(symbol, side, notional, client_order_id, reduce_only=True)

    # ---- state ----
    async def positions(self) -> list[Position]:
        async with self._lock:
            return [
                Position(symbol=s, side=p.side, qty=p.qty, entry_price=p.entry_price)
                for s, p in self._positions.items()
            ]

    async def account_state(self) -> AccountState:
        equity = self._equity
        for p in await self.positions():
            price = await self.mark_price(p.symbol)
            if price:
                sign = 1 if p.side == "long" else -1
                equity += (price - p.entry_price) * p.qty * sign
        return AccountState(equity_usd=equity, positions=await self.positions())

    async def mark_price(self, symbol: str) -> float | None:
        if self._price_provider:
            return await self._price_provider(symbol)
        pos = self._positions.get(symbol)
        return pos.entry_price if pos else None
