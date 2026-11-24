from __future__ import annotations

from dataclasses import dataclass, field

from copyflow_core.executors.base import Position
from copyflow_core.models import Action, Side, SignalEvent


@dataclass
class SizingDecision:
    notional_usd: float
    side: str  # buy | sell
    reduce_only: bool
    reason: str = ""


@dataclass
class CopyLimits:
    """Per-subscription risk config (mirrors db.Subscription)."""

    sizing: str = "equity_frac"  # equity_frac | fixed_usd | proportional
    fixed_usd: float = 0.0
    equity_usd: float = 1_000.0  # virtual / allocated capital
    leverage_cap: float = 3.0
    max_price_deviation_pct: float = 1.0
    slippage_bps: int = 10
    min_notional_usd: float = 10.0
    max_positions: int = 10
    blacklisted_symbols: list[str] = field(default_factory=list)
    daily_loss_limit_pct: float = 10.0
    max_drawdown_pct: float = 25.0


class SkipSignal(Exception):
    """Raised when a signal must not be executed; carries a reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def leader_equity(event: SignalEvent, leader_equity_usd: float) -> float:
    return leader_equity_usd


def resolve_action(event: SignalEvent, pos: Position | None) -> Action:
    """Map a leader action onto follower's actual position state."""
    if pos is None or pos.qty <= 0:
        if event.action == Action.CLOSE:
            return Action.CLOSE  # nothing to close -> no-op
        return Action.OPEN if event.action in (Action.OPEN, Action.INCREASE) else Action.OPEN

    same_side = pos.side == event.side.value
    if event.action in (Action.OPEN, Action.INCREASE):
        return Action.INCREASE if same_side else Action.FLIP
    if event.action == Action.REDUCE:
        return Action.REDUCE if same_side else Action.INCREASE
    if event.action == Action.FLIP:
        return Action.FLIP if not same_side else Action.REDUCE
    return Action.CLOSE


def price_deviation_pct(event: SignalEvent, ref_price: float) -> float:
    if not event.ref_price:
        return 0.0
    return abs(ref_price - event.ref_price) / event.ref_price * 100.0


def compute_size(
    event: SignalEvent,
    action: Action,
    limits: CopyLimits,
    leader_equity_usd: float,
    follower_positions: list[Position],
    ref_price: float,
    current_pos: Position | None,
    equity_now_usd: float | None = None,
) -> SizingDecision:
    """Compute follower's notional and side for a resolved action.

    Raises SkipSignal with a human-readable reason when execution is blocked.
    Risk breaches (daily loss / drawdown) are NOT enforced here: the server
    only detects and pushes them; the client decides what to do.
    """
    symbol = event.instrument.symbol

    if symbol.split("-")[0].upper() in {s.upper() for s in limits.blacklisted_symbols}:
        raise SkipSignal(f"symbol {symbol} blacklisted")

    if price_deviation_pct(event, ref_price) > limits.max_price_deviation_pct:
        raise SkipSignal(
            f"price moved {price_deviation_pct(event, ref_price):.2f}% since signal "
            f"(> {limits.max_price_deviation_pct}%)"
        )

    equity = equity_now_usd if equity_now_usd is not None else limits.equity_usd

    if action == Action.CLOSE:
        if current_pos is None or current_pos.qty <= 0:
            raise SkipSignal("no position to close")
        notional = current_pos.qty * ref_price
        return SizingDecision(
            notional_usd=notional,
            side="sell" if current_pos.side == "long" else "buy",
            reduce_only=True,
        )

    # leader's notional for this trade
    leader_notional = event.size_usd(leader_equity_usd)
    if leader_notional <= 0:
        raise SkipSignal("signal has no size (size_frac/size_abs_usd missing)")

    if action in (Action.OPEN, Action.INCREASE, Action.FLIP):
        # flip closes current position first; engine handles that separately,
        # sizing below is for the new exposure after the close
        if action == Action.OPEN and len(follower_positions) >= limits.max_positions and current_pos is None:
            raise SkipSignal(f"max positions reached ({limits.max_positions})")

        if limits.sizing == "fixed_usd":
            target_notional = limits.fixed_usd
        elif limits.sizing == "proportional" and current_pos is not None:
            target_notional = leader_notional
        else:  # equity_frac: mirror the leader's fraction of equity
            frac = leader_notional / leader_equity_usd
            target_notional = frac * equity

        # add to existing same-side position on increase
        if action == Action.INCREASE and current_pos is not None:
            target_notional = event.size_usd(leader_equity_usd) / leader_equity_usd * equity

        if target_notional < limits.min_notional_usd:
            raise SkipSignal(
                f"notional {target_notional:.2f} < min {limits.min_notional_usd}"
            )

        # leverage cap across all positions
        gross = sum(p.qty * ref_price for p in follower_positions if p.symbol != symbol)
        if current_pos is not None:
            gross += current_pos.qty * ref_price
        cap_notional = limits.leverage_cap * equity
        room = max(0.0, cap_notional - gross)
        notional = min(target_notional, room)
        if notional < limits.min_notional_usd:
            raise SkipSignal(
                f"leverage cap {limits.leverage_cap}x leaves no room "
                f"(gross {gross:.0f} / cap {cap_notional:.0f})"
            )

        return SizingDecision(
            notional_usd=notional,
            side="buy" if event.side == Side.LONG else "sell",
            reduce_only=False,
        )

    # REDUCE
    if current_pos is None or current_pos.qty <= 0:
        raise SkipSignal("no position to reduce")
    frac = min(1.0, event.size_frac or 1.0)
    reduce_qty = current_pos.qty * frac
    notional = reduce_qty * ref_price
    return SizingDecision(
        notional_usd=notional,
        side="sell" if current_pos.side == "long" else "buy",
        reduce_only=True,
    )
