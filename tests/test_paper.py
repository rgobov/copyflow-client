from __future__ import annotations

import pytest

from copyflow_core.executors.base import ExecutionError
from copyflow_core.executors.paper import PaperConfig, PaperExecutor


async def fake_price(symbol: str) -> float:
    return 100_000.0


async def make_executor():
    return PaperExecutor(PaperConfig(starting_equity_usd=1_000.0), price_provider=fake_price)


async def test_open_long_then_close_profit():
    ex = await make_executor()
    await ex.market_order("BTC", "buy", 500, "oid1")
    positions = await ex.positions()
    assert len(positions) == 1
    assert positions[0].side == "long"
    assert positions[0].qty == pytest.approx(0.005)

    # price goes to 110k
    async def p110(symbol):
        return 110_000.0

    ex._price_provider = p110
    fill = await ex.close_position("BTC", "oid2")
    assert fill is not None
    # pnl = (110k-100k)*0.005 = 50 minus fee
    state = await ex.account_state()
    assert state.equity_usd > 1040


async def test_flip_shorts_up():
    ex = await make_executor()
    await ex.market_order("BTC", "buy", 400, "oid1")
    await ex.market_order("BTC", "sell", 900, "oid2")  # close 400, open short 500
    positions = await ex.positions()
    assert len(positions) == 1
    assert positions[0].side == "short"
    assert positions[0].qty == pytest.approx(0.005)


async def test_close_nonexistent_is_noop():
    ex = await make_executor()
    assert await ex.close_position("BTC", "oid-x") is None


async def test_idempotent_guard():
    ex = await make_executor()
    await ex.market_order("BTC", "buy", 100, "same-id")
    with pytest.raises(ExecutionError):
        await ex.market_order("BTC", "buy", 100, "same-id")


async def test_fees_reduce_equity():
    ex = await make_executor()
    await ex.market_order("BTC", "buy", 100, "o1")
    await ex.market_order("BTC", "sell", 100, "o2")
    state = await ex.account_state()
    # 2 fills * 3.5 bps * 100 usd = 0.07 usd in fees
    assert state.equity_usd == pytest.approx(999.93, abs=0.01)
