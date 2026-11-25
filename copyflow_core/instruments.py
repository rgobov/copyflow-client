from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

MOEX_SECURITIES_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json"
    "?iss.meta=off&iss.only=securities,securities.columns"
)

MOEX_TTL = 24 * 3600.0

# static crypto registry: symbol -> (market, currency, lot_size, qty_decimals)
CRYPTO: dict[str, tuple[str, str, float, int]] = {
    sym: ("crypto", "USDT", 1e-4, 6)
    for sym in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON")
}


@dataclass(frozen=True)
class InstrumentSpec:
    venue: str
    symbol: str
    market: str  # crypto | ru | us | in
    currency: str  # quote currency: USDT/USD/RUB/INR
    lot_size: float = 1.0
    qty_decimals: int = 2


def normalize_currency(ccy: str | None) -> str:
    """MOEX uses legacy 'SUR' for the rouble; FX sources use 'RUB'."""
    if not ccy:
        return "RUB"
    return {"SUR": "RUB", "RUR": "RUB"}.get(ccy.upper(), ccy.upper())


def static_spec(venue: str, symbol: str) -> InstrumentSpec | None:
    base = symbol.split("-")[0].upper()
    if venue in ("binance", "hyperliquid") or base in CRYPTO:
        market, currency, lot, dec = CRYPTO.get(base, ("crypto", "USDT", 1e-4, 6))
        return InstrumentSpec(venue=venue, symbol=symbol, market=market,
                              currency=currency, lot_size=lot, qty_decimals=dec)
    if venue == "moex":
        return None  # dynamic registry
    if venue == "us":
        return InstrumentSpec(venue=venue, symbol=symbol, market="us",
                              currency="USD", lot_size=1.0, qty_decimals=2)
    if venue in ("india", "nse"):
        return InstrumentSpec(venue=venue, symbol=symbol, market="in",
                              currency="INR", lot_size=1.0, qty_decimals=2)
    return None


def looks_like_india(symbol: str) -> bool:
    return symbol.upper().endswith((".NS", ".BO"))


class InstrumentRegistry:
    """Static crypto/US/IN specs + dynamic MOEX board list (TQBR) with TTL."""

    def __init__(self, http: httpx.AsyncClient | None = None, moex_ttl: float = MOEX_TTL,
                 verify_tls: bool = True):
        if http is None:
            http = httpx.AsyncClient(timeout=10.0, verify=verify_tls)
        self._http = http
        self._moex_ttl = moex_ttl
        self._moex: dict[str, InstrumentSpec] = {}
        self._loaded_at: float = 0.0
        self._lock = __import__("asyncio").Lock()

    async def spec(self, venue: str, symbol: str) -> InstrumentSpec | None:
        static = static_spec(venue, symbol)
        if static:
            return static
        if venue == "moex":
            await self.ensure_moex()
            return self._moex.get(symbol.upper())
        if looks_like_india(symbol):
            return InstrumentSpec(venue=venue or "india", symbol=symbol, market="in",
                                  currency="INR", lot_size=1.0, qty_decimals=2)
        return None

    async def moex_tickers(self) -> set[str]:
        await self.ensure_moex()
        return set(self._moex)

    async def ensure_moex(self) -> None:
        if self._moex and time.time() - self._loaded_at < self._moex_ttl:
            return
        async with self._lock:
            if self._moex and time.time() - self._loaded_at < self._moex_ttl:
                return
            try:
                r = await self._http.get(MOEX_SECURITIES_URL)
                r.raise_for_status()
                data = r.json()
                cols = data["securities"]["columns"]
                rows = data["securities"]["data"]
                idx = {c: i for i, c in enumerate(cols)}
                specs: dict[str, InstrumentSpec] = {}
                for row in rows:
                    secid = str(row[idx["SECID"]])
                    specs[secid.upper()] = InstrumentSpec(
                        venue="moex", symbol=secid.upper(), market="ru",
                        currency=normalize_currency(row[idx.get("CURRENCYID")]),
                        lot_size=float(row[idx.get("LOTSIZE")] or 1),
                        qty_decimals=int(row[idx.get("DECIMALS")] or 0),
                    )
                if specs:
                    self._moex = specs
                    self._loaded_at = time.time()
                log.info("moex registry: %d securities loaded", len(specs))
            except Exception:
                log.exception("moex registry load failed")
