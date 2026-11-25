from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"
MOEX_MD_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{sym}.json"
    "?iss.meta=off&iss.only=marketdata,securities,marketdata.columns,securities.columns"
)
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

FX_TTL = 3600.0


class MultiMarketData:
    """Public market data across crypto / MOEX / US / India — no API keys.

    price(venue, symbol) routes to the right public source; venue="auto"
    routes by symbol pattern (MOEX registry, .NS/.BO suffix, crypto majors,
    everything else -> Yahoo which covers US and global markets).

    verify_tls=False disables certificate verification (some RU ISPs MITM
    public endpoints with self-signed certs; opt-in per deployment).
    """

    def __init__(self, http: httpx.AsyncClient | None = None, registry=None,
                 fx_ttl: float = FX_TTL, verify_tls: bool = True):
        if http is None:
            http = httpx.AsyncClient(timeout=10.0, verify=verify_tls)
        self._http = http
        self._registry = registry
        self._fx: dict[str, float] = {}  # ccy -> RUB per unit
        self._fx_at: float = 0.0
        self._fx_ttl = fx_ttl

    # ---- routing ----
    async def market_currency(self, symbol: str) -> tuple[str, str]:
        """Guess (market, quote currency) for a symbol: MOEX registry,
        .NS/.BO suffix -> India, crypto majors -> crypto, else US."""
        sym = symbol.upper()
        base = sym.split("-")[0]
        if self._registry:
            tickers = await self._registry.moex_tickers()
            if base in tickers:
                spec = await self._registry.spec("moex", base)
                return "ru", spec.currency if spec else "RUB"
        if sym.endswith((".NS", ".BO")):
            return "in", "INR"
        if base in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON"):
            return "crypto", "USDT"
        return "us", "USD"

    async def price(self, venue: str, symbol: str) -> float | None:
        sym = symbol.upper()
        if venue == "moex":
            return await self._moex_price(sym)
        if venue == "binance":
            return await self._binance_price(sym)
        if venue in ("us", "india", "nse"):
            return await self._yahoo_price(sym)
        if venue in ("hyperliquid",):
            return None  # executor has its own allMids source
        return await self.price_by_symbol(sym)

    async def price_by_symbol(self, symbol: str) -> float | None:
        sym = symbol.upper()
        base = sym.split("-")[0]
        if self._registry:
            tickers = await self._registry.moex_tickers()
            if base in tickers:
                return await self._moex_price(base)
        if sym.endswith((".NS", ".BO")):
            return await self._yahoo_price(sym)
        if base in ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "TON"):
            return await self._binance_price(f"{base}USDT")
        return await self._yahoo_price(sym)

    # ---- sources ----
    async def _binance_price(self, symbol: str) -> float | None:
        """Accepts BTCUSDT or plain base (BTC -> BTCUSDT)."""
        sym = symbol.upper()
        if "-" in sym:
            sym = sym.split("-")[0]
        if "/" in sym:
            sym = sym.replace("/", "")
        if not sym.endswith(("USDT", "USDC", "BUSD", "FDUSD")):
            sym = f"{sym}USDT"
        try:
            r = await self._http.get(BINANCE_URL, params={"symbol": sym})
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception as e:
            log.warning("binance price %s: %s", sym, e)
            return None

    async def _moex_price(self, symbol: str) -> float | None:
        try:
            r = await self._http.get(MOEX_MD_URL.format(sym=symbol))
            r.raise_for_status()
            data = r.json()
            md = data.get("marketdata", {})
            cols = md.get("columns", [])
            rows = md.get("data", [])
            if rows:
                idx = {c: i for i, c in enumerate(cols)}
                last = rows[0][idx["LAST"]]
                if last:
                    return float(last)
            sec = data.get("securities", {})
            scols = sec.get("columns", [])
            srows = sec.get("data", [])
            if srows:
                sidx = {c: i for i, c in enumerate(scols)}
                prev = srows[0][sidx.get("PREVPRICE")]
                if prev:
                    return float(prev)
        except Exception as e:
            log.warning("moex price %s: %s", symbol, e)
        return None

    async def _yahoo_price(self, symbol: str) -> float | None:
        try:
            r = await self._http.get(
                YAHOO_URL.format(sym=symbol),
                headers={"User-Agent": "Mozilla/5.0 (copyflow)"},
            )
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            return float(price) if price is not None else None
        except Exception as e:
            log.warning("yahoo price %s: %s", symbol, e)
            return None

    # ---- FX ----
    async def fx_rates(self) -> dict[str, float]:
        """ccy -> RUB per 1 unit (RUB itself = 1). Cached."""
        if self._fx and time.time() - self._fx_at < self._fx_ttl:
            return self._fx
        try:
            r = await self._http.get(CBR_URL)
            r.raise_for_status()
            valutes = r.json()["Valute"]
            rates = {"RUB": 1.0}
            for code, v in valutes.items():
                rates[code] = float(v["Value"]) / float(v["Nominal"])
            self._fx = rates
            self._fx_at = time.time()
        except Exception as e:
            log.warning("cbr fx fetch: %s", e)
        return self._fx

    async def fx_rate(self, from_ccy: str, to_ccy: str) -> float | None:
        if from_ccy == to_ccy:
            return 1.0
        if from_ccy in ("USD", "USDT") and to_ccy in ("USD", "USDT"):
            return 1.0
        rates = await self.fx_rates()
        rub_from = rates.get(from_ccy)
        rub_to = rates.get(to_ccy)
        if not rub_from or not rub_to:
            return None
        return rub_from / rub_to

    async def convert(self, amount: float, from_ccy: str, to_ccy: str) -> float | None:
        rate = await self.fx_rate(from_ccy, to_ccy)
        return amount * rate if rate is not None else None
