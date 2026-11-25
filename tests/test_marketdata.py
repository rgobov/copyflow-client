from __future__ import annotations

import httpx
import pytest

from copyflow_core.instruments import InstrumentRegistry, static_spec
from copyflow_core.marketdata import MultiMarketData


def make_http(routes: dict[str, dict]):
    """httpx client with canned JSON responses keyed by URL substring."""
    def handler(request: httpx.Request) -> httpx.Response:
        for frag, payload in routes.items():
            if frag in str(request.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "no route", "url": str(request.url)})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


MOEX_SEC = {
    "securities": {
        "columns": ["SECID", "LOTSIZE", "DECIMALS", "CURRENCYID", "PREVPRICE"],
        "data": [["SBER", 10, 2, "RUB", 285.5], ["GAZP", 10, 2, "RUB", 130.1]],
    }
}
MOEX_MD = {
    "marketdata": {"columns": ["LAST"], "data": [[290.4]]},
    "securities": {"columns": ["PREVPRICE"], "data": [[285.5]]},
}
BINANCE = {"symbol": "BTCUSDT", "price": "95000.5"}
YAHOO_AAPL = {"chart": {"result": [{"meta": {"regularMarketPrice": 231.4}}]}}
CBR = {"Valute": {"USD": {"Nominal": 1, "Value": 90.0},
                  "EUR": {"Nominal": 1, "Value": 99.0},
                  "INR": {"Nominal": 10, "Value": 10.8}}}


async def test_static_specs():
    btc = static_spec("binance", "BTC")
    assert btc and btc.market == "crypto" and btc.currency == "USDT"
    us = static_spec("us", "AAPL")
    assert us.market == "us" and us.currency == "USD"


async def test_moex_registry_and_price():
    http = make_http({"/iss/engines/stock/markets/shares/boards/TQBR/securities.json": MOEX_SEC,
                      "securities/SBER.json": MOEX_MD})
    reg = InstrumentRegistry(http=http)
    spec = await reg.spec("moex", "SBER")
    assert spec is not None
    assert spec.market == "ru" and spec.currency == "RUB" and spec.lot_size == 10

    tickers = await reg.moex_tickers()
    assert "SBER" in tickers and "GAZP" in tickers

    md = MultiMarketData(http=http, registry=reg)
    assert await md.price("moex", "SBER") == pytest.approx(290.4)


async def test_price_routing_by_venue():
    http = make_http({
        "api.binance.com": BINANCE,
        "query1.finance.yahoo.com": YAHOO_AAPL,
        "securities/SBER.json": MOEX_MD,
    })
    md = MultiMarketData(http=http)
    assert await md.price("binance", "BTC") == pytest.approx(95000.5)
    assert await md.price("us", "AAPL") == pytest.approx(231.4)


async def test_price_routing_by_symbol():
    routes = {
        "api.binance.com": BINANCE,
        "query1.finance.yahoo.com": YAHOO_AAPL,
        "securities/SBER.json": MOEX_MD,
        "iss/engines/stock/markets/shares/boards/TQBR/securities.json": MOEX_SEC,
    }
    http = make_http(routes)
    reg = InstrumentRegistry(http=http)
    md = MultiMarketData(http=http, registry=reg)

    assert await md.price_by_symbol("BTC") == pytest.approx(95000.5)   # crypto major
    assert await md.price_by_symbol("SBER") == pytest.approx(290.4)    # moex registry
    assert await md.price_by_symbol("AAPL") == pytest.approx(231.4)    # yahoo (us)
    assert await md.price_by_symbol("RELIANCE.NS") == pytest.approx(231.4)  # yahoo (in)


async def test_fx_convert():
    http = make_http({"cbr-xml-daily": CBR})
    md = MultiMarketData(http=http)
    assert await md.convert(100, "USD", "USD") == pytest.approx(100)
    assert await md.convert(90, "RUB", "USD") == pytest.approx(1.0)
    assert await md.convert(1, "USD", "RUB") == pytest.approx(90.0)
    # INR nominal 10 -> 1.08 INR per RUB; 1 USD = 90 RUB = 83.33 INR
    assert await md.convert(1, "USD", "INR") == pytest.approx(83.333, rel=1e-3)
