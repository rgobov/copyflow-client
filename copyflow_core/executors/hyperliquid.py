from __future__ import annotations

import asyncio
import logging

import httpx

from copyflow_core.executors.base import AccountState, ExecutionAdapter, ExecutionError, Fill, Position

log = logging.getLogger(__name__)

INFO_TIMEOUT = 10.0
TAKER_FEE = 0.00045


class HyperliquidExecutor(ExecutionAdapter):
    """Hyperliquid perps via agent/API wallet (no withdraw rights).

    State is read from the public info endpoint. Orders are placed through the
    hyperliquid-python-sdk (IOC limit at slipped price ~= market). Idempotency
    is enforced upstream by the copy engine (unique client_order_id per signal);
    the SDK's order call does not accept a client id.
    """

    venue = "hyperliquid"

    def __init__(
        self,
        api_url: str,
        agent_private_key: str,
        agent_address: str | None = None,
        builder_code: str | None = None,
    ):
        self._api_url = api_url.rstrip("/")
        self._pk = agent_private_key
        self._address = agent_address
        self._builder = builder_code
        self._client = httpx.AsyncClient(timeout=INFO_TIMEOUT)
        self._nonce_lock = asyncio.Lock()
        self._sz_decimals_cache: dict[str, int] | None = None

    # ---- info ----
    async def _info(self, payload: dict) -> dict:
        r = await self._client.post(f"{self._api_url}/info", json=payload)
        r.raise_for_status()
        return r.json()

    async def _user_state(self) -> dict:
        if not self._address:
            raise ExecutionError("hyperliquid: agent address not configured")
        return await self._info({"type": "clearinghouseState", "user": self._address})

    @staticmethod
    def _coin_of(symbol: str) -> str:
        return symbol.split("-")[0].upper()

    async def positions(self) -> list[Position]:
        state = await self._user_state()
        out: list[Position] = []
        for item in state.get("assetPositions", []):
            pos = item.get("position", {})
            szi = float(pos.get("szi", 0))
            if abs(szi) < 1e-12:
                continue
            out.append(
                Position(
                    symbol=pos.get("coin", ""),
                    side="long" if szi > 0 else "short",
                    qty=abs(szi),
                    entry_price=float(pos.get("entryPx") or 0),
                )
            )
        return out

    async def account_state(self) -> AccountState:
        state = await self._user_state()
        margin = state.get("marginSummary", {})
        equity = float(margin.get("accountValue", 0))
        return AccountState(equity_usd=equity, positions=await self.positions())

    async def mark_price(self, symbol: str) -> float | None:
        data = await self._info({"type": "allMids"})
        mids = data.get("mids", {})
        for key in (symbol, self._coin_of(symbol)):
            if key in mids:
                return float(mids[key])
        return None

    async def _sz_decimals_map(self) -> dict[str, int]:
        if self._sz_decimals_cache is None:
            meta = await self._info({"type": "meta"})
            self._sz_decimals_cache = {
                a["name"]: int(a.get("szDecimals", 2)) for a in meta.get("universe", [])
            }
        return self._sz_decimals_cache

    # ---- exchange (orders) ----
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
        try:
            import eth_account
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
        except ImportError as e:  # pragma: no cover
            raise ExecutionError(
                "hyperliquid: pip install hyperliquid-python-sdk to place live orders"
            ) from e

        coin = self._coin_of(symbol)
        price = ref_price or await self.mark_price(symbol)
        if not price:
            raise ExecutionError(f"hyperliquid: no mark price for {symbol}")

        sz_decimals = (await self._sz_decimals_map()).get(coin)
        if sz_decimals is None:
            raise ExecutionError(f"hyperliquid: unknown coin {coin}")

        slip = slippage_bps / 10_000.0
        px = price * (1 + slip) if side == "buy" else price * (1 - slip)
        px = float(f"{px:.{5 - sz_decimals}g}")
        qty = float(f"{notional_usd / price:.{sz_decimals}f}")
        if qty <= 0:
            raise ExecutionError(f"hyperliquid: size too small for {symbol} ({notional_usd} usd)")

        account = eth_account.Account.from_key(self._pk)
        base_url = constants.MAINNET_API_URL if "mainnet" in self._api_url else self._api_url
        exchange = Exchange(account, base_url, account_address=self._address)

        is_buy = side == "buy"
        order_type = {"limit": {"tif": "Ioc"}}
        builder = {"b": self._builder, "f": 5} if self._builder else None

        async with self._nonce_lock:
            result = exchange.order(
                coin,
                is_buy,
                qty,
                px,
                order_type,
                reduce_only=reduce_only,
                builder=builder,
            )
        log.info("hl.order %s -> %s", client_order_id, result)

        statuses = (
            result.get("response", {}).get("data", {}).get("statuses") or []
            if isinstance(result, dict)
            else []
        )
        if not statuses:
            raise ExecutionError(f"hyperliquid: empty order response: {result}")
        filled = statuses[0].get("filled")
        if not filled:
            err = statuses[0].get("error", "no fill")
            raise ExecutionError(f"hyperliquid: order not filled: {err}")
        fill_qty = float(filled["totalSz"])
        fill_px = float(filled["avgPx"])
        fee_usd = fill_qty * fill_px * TAKER_FEE
        return Fill(symbol=symbol, side=side, qty=fill_qty, price=fill_px, fee_usd=fee_usd)

    async def close_position(self, symbol: str, client_order_id: str) -> Fill | None:
        coin = self._coin_of(symbol)
        for pos in await self.positions():
            if pos.symbol == coin:
                side = "sell" if pos.side == "long" else "buy"
                notional = pos.qty * (await self.mark_price(symbol) or pos.entry_price)
                return await self.market_order(
                    symbol, side, notional, client_order_id, reduce_only=True, ref_price=pos.entry_price
                )
        return None

    async def aclose(self) -> None:
        await self._client.aclose()
