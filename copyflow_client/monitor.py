from __future__ import annotations

import asyncio
import logging

from copyflow_client.processor import Processor

log = logging.getLogger(__name__)


class ClientMonitor:
    """Watches equity per subscription; pauses/flattens on breach (client's own money)."""

    def __init__(self, processor: Processor, interval: float = 10.0):
        self._processor = processor
        self._interval = interval
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="client-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception:
                log.exception("client monitor pass failed")
            await asyncio.sleep(self._interval)

    async def check_once(self) -> list[dict]:
        from copyflow_client import risk as crisk

        breaches = []
        for sub_id, cfg in self._processor.subs.items():
            adapter = self._processor.adapter(cfg)
            try:
                equity = (await adapter.account_state()).equity_usd
            except Exception as e:
                log.error("client monitor: cannot read account sub=%s: %s", sub_id, e)
                continue

            async with self._processor.session_factory() as session:
                day, risk = await crisk.assess_risk(session, cfg, equity)

                if risk.blocked and not risk.paused:
                    await self._processor._on_breach(session, cfg, day, risk)
                    breaches.append({
                        "subscription_id": sub_id,
                        "equity_usd": equity,
                        "reason": risk.reason,
                    })
                elif not risk.blocked:
                    # reset notification edges on recovery
                    day.daily_loss_notified = False
                    day.drawdown_notified = False
                await session.commit()
        return breaches
