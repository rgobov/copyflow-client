from __future__ import annotations

import logging

from copyflow_core.executors.base import AccountState, ExecutionAdapter, ExecutionError, Fill, Position

log = logging.getLogger(__name__)


def _load_ccxt():
    try:
        import ccxt.async_support as ccxt

        return ccxt
    except ImportError as e:  # pragma: no cover
        raise ExecutionError("binance: pip install ccxt to place live orders") from e


class BinanceExecutor(ExecutionAdapter):
    """Binance USD-M futures via ccxt, trade-only API keys.

    Idempotency: ccxt params.newClientOrderId truncated to 36 chars.
    """

    venue = "binance"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        quote: str = "USDT",
    ):
        ccxt = _load_ccxt()
        self._quote = quote
        self._ccxt = ccxt
        self._client = ccxt.binanceusdm(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            }
        )
        if testnet:
            self._client.set_sandbox_mode(True)

    @staticmethod
    def _ccxt_symbol(symbol: str) -> str:
        base = symbol.split("-")[0].upper()
        return f"{base}/USDT:USDT"

    async def market_order(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        client_order_id: str,
        reduce_only: bool = False,
        ref_price: float | None = None,
        slippage_bps: int = 10,
    ) -> Fill:
        client = self._client
        ccxt_symbol = self._ccxt_symbol(symbol)
        try:
            market = client.market(ccxt_symbol)
        except Exception as e:
            raise ExecutionError(f"binance: unknown symbol {ccxt_symbol}: {e}") from e

        price = ref_price or await self.mark_price(symbol)
        if not price:
            raise ExecutionError(f"binance: no price for {symbol}")
        amount = notional_usd / price
        amount = float(client.amount_to_precision(ccxt_symbol, amount))
        if amount <= 0:
            raise ExecutionError(f"binance: size too small for {symbol}")

        params = {
            "newClientOrderId": client_order_id[:36],
            "reduceOnly": reduce_only,
        }
        try:
            order = await client.create_order(
                ccxt_symbol, "market", side, amount, None, params
            )
        except Exception as e:
            raise ExecutionError(f"binance: order failed: {e}") from e

        filled = float(order.get("filled") or 0) or amount
        avg = float(order.get("average") or price)
        taker = float(market.get("taker") or 0.00045)
        return Fill(symbol=symbol, side=side, qty=filled, price=avg, fee_usd=filled * avg * taker)

    async def positions(self) -> list[Position]:
        client = self._client
        raw = await client.fetch_positions()
        out: list[Position] = []
        for p in raw:
            contracts = float(p.get("contracts") or 0)
            if abs(contracts) < 1e-12:
                continue
            out.append(
                Position(
                    symbol=(p.get("symbol") or "").split("/")[0],
                    side=p.get("side") or "long",
                    qty=abs(contracts),
                    entry_price=float(p.get("entryPrice") or 0),
                )
            )
        return out

    async def account_state(self) -> AccountState:
        client = self._client
        balance = await client.fetch_balance()
        equity = float(balance.get(self._quote, {}).get("total", 0) or 0)
        return AccountState(equity_usd=equity, positions=await self.positions())

    async def close_position(self, symbol: str, client_order_id: str) -> Fill | None:
        for pos in await self.positions():
            if pos.symbol == symbol.split("-")[0].upper():
                side = "sell" if pos.side == "long" else "buy"
                notional = pos.qty * (await self.mark_price(symbol) or pos.entry_price)
                return await self.market_order(
                    symbol, side, notional, client_order_id, reduce_only=True, ref_price=pos.entry_price
                )
        return None

    async def mark_price(self, symbol: str) -> float | None:
        try:
            t = await self._client.fetch_ticker(self._ccxt_symbol(symbol))
            return float(t.get("last") or 0) or None
        except Exception:
            return None

    async def aclose(self) -> None:
        await self._client.close()
