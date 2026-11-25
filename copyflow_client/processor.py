from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from copyflow_client import risk as crisk
from copyflow_client.config import SubConfig
from copyflow_client.models import Decision, DecisionStatus, Fill, Order, OrderStatus
from copyflow_core.executors.base import ExecutionAdapter, ExecutionError
from copyflow_core.models import Action, SignalEvent
from copyflow_core.sizing import CopyLimits, SkipSignal, compute_size, resolve_action

log = logging.getLogger(__name__)


def limits_from(cfg: SubConfig) -> CopyLimits:
    return CopyLimits(
        sizing=cfg.sizing,
        fixed_usd=cfg.fixed_usd,
        equity_usd=cfg.equity_usd,
        leverage_cap=cfg.leverage_cap,
        max_price_deviation_pct=cfg.max_price_deviation_pct,
        slippage_bps=cfg.slippage_bps,
        min_notional_usd=cfg.min_notional_usd,
        max_positions=cfg.max_positions,
        blacklisted_symbols=cfg.blacklisted_symbols,
        daily_loss_limit_pct=cfg.daily_loss_limit_pct,
        max_drawdown_pct=cfg.max_drawdown_pct,
    )


class Processor:
    """Decision pipeline: received signal -> own risk -> own sizing -> own execution."""

    def __init__(self, session_factory: async_sessionmaker, subs: dict[int, SubConfig],
                 adapters: dict[int, ExecutionAdapter], notifier=None):
        self._session_factory = session_factory
        self._subs = subs
        self._adapters = adapters
        self._notifier = notifier

    @property
    def session_factory(self) -> async_sessionmaker:
        return self._session_factory

    @property
    def subs(self) -> dict[int, SubConfig]:
        return self._subs

    @property
    def settings(self) -> "ClientSettings":
        from copyflow_client.config import get_settings

        return get_settings()

    def adapter(self, cfg: SubConfig) -> ExecutionAdapter:
        if cfg.subscription_id not in self._adapters:
            from copyflow_client.executor import build_adapter

            self._adapters[cfg.subscription_id] = build_adapter(cfg, self._settings)
        return self._adapters[cfg.subscription_id]

    @property
    def _settings(self):
        from copyflow_client.config import get_settings

        return get_settings()

    # ---- entry ----
    async def process(self, payload: dict) -> dict:
        sub_id = int(payload["subscription_id"])
        cfg = self._subs.get(sub_id)
        if cfg is None:
            log.warning("no local config for subscription %s; ignoring", sub_id)
            return {"status": "skipped", "reason": "no local config"}

        event = SignalEvent.model_validate(payload["signal"])
        leader_equity = float(payload.get("leader_equity_usd") or 10_000.0)

        async with self._session_factory() as session:
            day, risk = await self._risk(session, cfg)
            if risk.blocked:
                await self._on_breach(session, cfg, day, risk)
                return await self._record(session, event, DecisionStatus.SKIPPED,
                                          f"risk: {risk.reason}")

            if cfg.confirm_mode in ("confirm", "manual"):
                return await self._ask_user(session, cfg, event, payload, risk)

            return await self._execute(session, cfg, event, leader_equity, day)

    # ---- execution (auto or confirmed) ----
    async def _execute(self, session, cfg: SubConfig, event: SignalEvent,
                       leader_equity: float, day) -> dict:
        adapter = self.adapter(cfg)
        symbol = event.instrument.symbol

        # follower portfolio allocation: share of total capital (strict)
        effective_equity = (await adapter.account_state()).equity_usd
        if self._settings.total_capital_usd > 0:
            effective_equity = self._settings.total_capital_usd * (cfg.allocation_pct or 0.0) / 100.0
            if effective_equity <= 0:
                return await self._record(session, event, DecisionStatus.SKIPPED,
                                          "no allocation (set allocation_pct > 0)")

        try:
            positions = await adapter.positions()
            current_pos = crisk.resolve_current(positions, symbol)
            ref_price = await adapter.mark_price(symbol) or event.ref_price
            action = resolve_action(event, current_pos)
            decision = compute_size(
                event, action, limits_from(cfg), leader_equity,
                positions, ref_price or 0.0, current_pos, equity_now_usd=effective_equity,
            )
            # client-side absolute cap
            if action in (Action.OPEN, Action.INCREASE, Action.FLIP):
                decision.notional_usd = min(decision.notional_usd, cfg.max_notional_usd)
                if decision.notional_usd < cfg.min_notional_usd:
                    raise SkipSignal("notional below min after cap")
        except SkipSignal as e:
            return await self._record(session, event, DecisionStatus.SKIPPED, e.reason)

        coid = f"cl{event.id[:20]}{cfg.subscription_id}"
        await self._flip_close(session, adapter, action, current_pos, ref_price, coid)

        order = Order(
            client_order_id=coid, signal_id=event.id, venue=adapter.venue,
            symbol=symbol, side=decision.side, kind=action.value,
            notional_usd=decision.notional_usd, status=OrderStatus.NEW,
        )
        session.add(order)
        await session.flush()
        try:
            fill = await adapter.market_order(
                symbol=symbol, side=decision.side, notional_usd=decision.notional_usd,
                client_order_id=coid, reduce_only=decision.reduce_only,
                ref_price=ref_price, slippage_bps=cfg.slippage_bps,
            )
        except ExecutionError as e:
            order.status = OrderStatus.FAILED
            order.reason = str(e)[:250]
            await session.commit()
            await self._notify(f"❌ client order failed {symbol}: {e}")
            return await self._record(session, event, DecisionStatus.SKIPPED, f"execution failed: {e}")

        order.status = OrderStatus.FILLED
        session.add(Fill(order_id=order.id, symbol=fill.symbol, side=fill.side,
                         qty=fill.qty, price=fill.price, fee_usd=fill.fee_usd))
        await session.commit()
        await self._notify(
            f"✅ EXECUTED {action.value} {symbol} {decision.side.upper()} "
            f"${decision.notional_usd:.0f} @ {fill.price:.4f} [{cfg.executor}]"
        )
        return await self._record(session, event, DecisionStatus.EXECUTED,
                                  notional=decision.notional_usd)

    async def _flip_close(self, session, adapter, action, current_pos, ref_price, coid):
        if action != Action.FLIP or current_pos is None:
            return
        close_side = "sell" if current_pos.side == "long" else "buy"
        notional = current_pos.qty * (ref_price or current_pos.entry_price)
        try:
            await adapter.market_order(current_pos.symbol, close_side, notional,
                                       coid + "-c", reduce_only=True,
                                       ref_price=ref_price)
        except ExecutionError as e:
            log.error("client flip close failed: %s", e)

    # ---- confirm / manual ----
    async def _ask_user(self, session, cfg, event, payload, risk) -> dict:
        decision = Decision(signal_id=event.id, status=DecisionStatus.AWAITING,
                            action=event.action.value)
        session.add(decision)
        await session.commit()
        text = (
            f"🔔 SIGNAL {event.action.value.upper()} {event.instrument.symbol} "
            f"{event.side.value} (leader {payload.get('leader_name', event.leader_id)})\n"
            f"day pnl {risk.day_pnl_pct:.1f}% | dd {risk.drawdown_pct:.1f}%"
        )
        if self._notifier:
            await self._notifier.ask(
                text, signal_id=event.id,
                buttons=[("Execute", f"cf:exec:{event.id}"), ("Skip", f"cf:skip:{event.id}")],
            )
        else:
            log.info("confirm needed signal=%s (%s) — no telegram configured", event.id, text)
        return {"status": "awaiting_confirm", "signal_id": event.id}

    async def execute_confirmed(self, signal_id: str, accept: bool) -> dict:
        """Telegram button callback."""
        from copyflow_client.models import ReceivedSignal

        async with self._session_factory() as session:
            d = (await session.execute(
                select(Decision).where(Decision.signal_id == signal_id)
            )).scalar_one_or_none()
            if d is None or d.status != DecisionStatus.AWAITING:
                return {"status": "stale"}
            if not accept:
                d.status = DecisionStatus.DECLINED
                await session.commit()
                await self._notify(f"🚫 skipped signal {signal_id[:12]}")
                return {"status": "declined"}

            rs = (await session.execute(
                select(ReceivedSignal).where(ReceivedSignal.signal_id == signal_id)
            )).scalar_one_or_none()
            if rs is None:
                return {"status": "stale"}
            payload = rs.payload
            sub_id = int(payload["subscription_id"])
            cfg = self._subs.get(sub_id)
            if cfg is None:
                return {"status": "skipped", "reason": "no local config"}
            event = SignalEvent.model_validate(payload["signal"])
            day, _ = await self._risk(session, cfg)
            return await self._execute(session, cfg, event,
                                       float(payload.get("leader_equity_usd") or 10_000.0), day)

    async def expire_stale(self) -> int:
        from sqlalchemy import select as sel

        n = 0
        async with self._session_factory() as session:
            rows = (await session.execute(
                sel(Decision).where(Decision.status == DecisionStatus.AWAITING)
            )).scalars().all()
            for d in rows:
                age = time.time() - d.created_at.timestamp()
                if age > self._settings.confirm_ttl_sec:
                    d.status = DecisionStatus.EXPIRED
                    n += 1
            await session.commit()
        return n

    # ---- helpers ----
    async def _risk(self, session: AsyncSession, cfg: SubConfig):
        adapter = self.adapter(cfg)
        equity = (await adapter.account_state()).equity_usd
        return await crisk.assess_risk(session, cfg, equity)

    async def _on_breach(self, session, cfg, day, risk: crisk.ClientRisk):

        if risk.paused:
            return
        reason = f"risk breach: {risk.reason}"
        if cfg.on_breach in ("pause", "pause_and_close"):
            await crisk.pause(session, day, reason)
        if cfg.on_breach == "pause_and_close":
            adapter = self.adapter(cfg)
            closed = await crisk.flatten_all(session, cfg, adapter, risk.reason)
            await self._notify(f"🛑 BREACH {reason} — flattened {closed} positions")
        elif cfg.on_breach == "notify_only":
            await self._notify(f"🛑 BREACH {reason}")
        log.error("CLIENT RISK BREACH sub=%s: %s", cfg.subscription_id, reason)

    async def _record(self, session, event, status: DecisionStatus, reason: str = "",
                      notional: float = 0.0) -> dict:
        existing = None
        if status != DecisionStatus.AWAITING:
            existing = (await session.execute(
                select(Decision).where(
                    Decision.signal_id == event.id,
                    Decision.status == DecisionStatus.AWAITING,
                )
            )).scalars().first()
        if existing:
            existing.status = status
            existing.reason = reason[:250]
            existing.notional_usd = notional
        else:
            session.add(Decision(signal_id=event.id, status=status, action=event.action.value,
                                 reason=reason[:250], notional_usd=notional))
        await session.commit()
        return {"status": status.value, "reason": reason, "signal_id": event.id}

    async def _notify(self, text: str) -> None:
        if self._notifier:
            await self._notifier.notify(text)
        else:
            log.info("client notify: %s", text)
