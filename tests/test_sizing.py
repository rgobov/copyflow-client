from __future__ import annotations

import pytest

from copyflow_core.executors.base import Position
from copyflow_core.models import Action, Instrument, Side, SignalEvent
from copyflow_core.sizing import CopyLimits, SkipSignal, compute_size, resolve_action


def sig(action: Action, side: Side = Side.LONG, frac: float | None = 0.5, abs_usd: float | None = None) -> SignalEvent:
    return SignalEvent(
        leader_id=1,
        instrument=Instrument(venue="paper", symbol="BTC"),
        action=action,
        side=side,
        size_frac=frac,
        size_abs_usd=abs_usd,
        ref_price=100_000.0,
    )


LIMITS = CopyLimits()


def test_open_equity_frac_sizing():
    d = compute_size(sig(Action.OPEN), Action.OPEN, LIMITS, leader_equity_usd=10_000,
                     follower_positions=[], ref_price=100_000, current_pos=None)
    # leader trades 50% of 10k = 5k -> follower 50% of 1k = 500
    assert d.notional_usd == pytest.approx(500)
    assert d.side == "buy"
    assert not d.reduce_only


def test_open_fixed_usd():
    limits = CopyLimits(sizing="fixed_usd", fixed_usd=250)
    d = compute_size(sig(Action.OPEN), Action.OPEN, limits, 10_000, [], 100_000, None)
    assert d.notional_usd == pytest.approx(250)


def test_price_deviation_guard():
    with pytest.raises(SkipSignal) as e:
        compute_size(sig(Action.OPEN), Action.OPEN, LIMITS, 10_000, [], 102_500, None)
    assert "price moved" in e.value.reason


def test_blacklist():
    limits = CopyLimits(blacklisted_symbols=["btc"])
    with pytest.raises(SkipSignal) as e:
        compute_size(sig(Action.OPEN), Action.OPEN, limits, 10_000, [], 100_000, None)
    assert "blacklisted" in e.value.reason


def test_min_notional():
    limits = CopyLimits(min_notional_usd=100)
    with pytest.raises(SkipSignal) as e:
        compute_size(
            sig(Action.OPEN, frac=0.002), Action.OPEN, limits, 10_000, [], 100_000, None
        )
    assert "min" in e.value.reason


def test_leverage_cap_limits_size():
    limits = CopyLimits(leverage_cap=1.0, equity_usd=1_000)
    pos = [Position(symbol="ETH", side="long", qty=0.0075, entry_price=100_000)]  # 750 usd gross
    d = compute_size(sig(Action.OPEN, frac=0.9), Action.OPEN, limits, 10_000, pos, 100_000, None)
    assert d.notional_usd == pytest.approx(250)  # cap 1000 - gross 750


def test_leverage_cap_blocks():
    limits = CopyLimits(leverage_cap=1.0, equity_usd=1_000)
    pos = [Position(symbol="ETH", side="long", qty=0.0099, entry_price=100_000)]
    with pytest.raises(SkipSignal) as e:
        compute_size(sig(Action.OPEN), Action.OPEN, limits, 10_000, pos, 100_000, None)
    assert "leverage cap" in e.value.reason


def test_max_positions():
    limits = CopyLimits(max_positions=1)
    pos = [Position(symbol="ETH", side="long", qty=0.01, entry_price=100_000)]
    with pytest.raises(SkipSignal) as e:
        compute_size(sig(Action.OPEN), Action.OPEN, limits, 10_000, pos, 100_000, None)
    assert "max positions" in e.value.reason


def test_reduce_uses_fraction():
    pos = Position(symbol="BTC", side="long", qty=0.01, entry_price=100_000)
    d = compute_size(sig(Action.REDUCE, frac=0.5), Action.REDUCE, LIMITS, 10_000, [pos], 100_000, pos)
    assert d.notional_usd == pytest.approx(0.005 * 100_000)
    assert d.side == "sell"
    assert d.reduce_only


def test_close_full_position():
    pos = Position(symbol="BTC", side="long", qty=0.01, entry_price=100_000)
    d = compute_size(sig(Action.CLOSE, frac=1.0), Action.CLOSE, LIMITS, 10_000, [pos], 100_000, pos)
    assert d.notional_usd == pytest.approx(0.01 * 100_000)
    assert d.reduce_only


# ---- resolve_action ----

def test_resolve_open_on_flat_is_open():
    assert resolve_action(sig(Action.OPEN), None) == Action.OPEN


def test_resolve_increase_same_side():
    pos = Position(symbol="BTC", side="long", qty=0.01, entry_price=100_000)
    assert resolve_action(sig(Action.INCREASE), pos) == Action.INCREASE


def test_resolve_increase_opposite_is_flip():
    pos = Position(symbol="BTC", side="short", qty=0.01, entry_price=100_000)
    assert resolve_action(sig(Action.INCREASE, side=Side.LONG), pos) == Action.FLIP


def test_resolve_flip_same_side_is_reduce():
    pos = Position(symbol="BTC", side="long", qty=0.01, entry_price=100_000)
    assert resolve_action(sig(Action.FLIP, side=Side.LONG), pos) == Action.REDUCE
