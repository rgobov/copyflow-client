from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select

from copyflow_client.config import ClientSettings, SubConfig
from copyflow_client.db import ensure_schema, make_engine, make_session_factory
from copyflow_client.models import Decision, ReceivedSignal
from copyflow_client.processor import Processor
from copyflow_core.webhooks import sign

log = logging.getLogger(__name__)


def create_app(settings: ClientSettings, subs: dict[int, SubConfig]) -> FastAPI:
    app = FastAPI(title="copyflow-client")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from copyflow_client.monitor import ClientMonitor
        from copyflow_client.telegram import ClientNotifier

        engine = make_engine(settings)
        await ensure_schema(engine)
        SF = make_session_factory(engine)

        notifier = ClientNotifier(settings)
        processor = Processor(SF, subs, adapters={}, notifier=notifier)
        app.state.processor = processor

        monitor = ClientMonitor(processor, interval=settings.risk_check_interval_sec)
        monitor.start()

        async def on_callback(action: str, signal_id: str):
            result = await processor.execute_confirmed(signal_id, accept=(action == "exec"))
            log.info("callback %s %s -> %s", action, signal_id[:12], result)

        polling = asyncio.create_task(notifier.run_polling(on_callback))
        expiring = asyncio.create_task(_expire_loop(processor))
        log.info("copyflow-client started on %s:%s (%d subscriptions)",
                 settings.listen_host, settings.listen_port, len(subs))
        yield
        polling.cancel()
        expiring.cancel()
        await monitor.stop()
        log.info("copyflow-client stopped")

    @app.post("/hook")
    async def hook(request: Request):
        processor = request.app.state.processor
        body = await request.body()
        sig = request.headers.get("X-Copyflow-Signature", "")
        ts = request.headers.get("X-Copyflow-Timestamp", "")

        if not sig or not ts:
            raise HTTPException(403, "missing signature headers")
        if abs(time.time() - int(ts)) > settings.max_signal_age_sec:
            raise HTTPException(403, "stale timestamp (replay?)")
        if sig != sign(settings.webhook_secret, body, ts):
            raise HTTPException(403, "bad signature")

        payload = json.loads(body)
        if payload.get("type") != "signal":
            return {"accepted": True, "note": "non-signal payload ignored"}

        signal_id = payload["signal_id"]
        async with processor.session_factory() as session:
            if await session.get(ReceivedSignal, signal_id) is not None:
                return {"accepted": True, "note": "duplicate (idempotent)"}
            session.add(ReceivedSignal(
                signal_id=signal_id,
                subscription_id=int(payload["subscription_id"]),
                payload=payload,
            ))
            await session.commit()

        result = await processor.process(payload)
        log.info("hook processed signal=%s -> %s", signal_id[:12], result.get("status"))
        return {"accepted": True, "result": result}

    @app.post("/resume")
    async def resume_all(request: Request):
        from copyflow_client import risk as crisk

        processor = request.app.state.processor
        resumed = []
        for sub_id, cfg in processor.subs.items():
            async with processor.session_factory() as session:
                if await crisk.resume(session, cfg):
                    resumed.append(sub_id)
                    await session.commit()
        return {"resumed": resumed}

    @app.get("/status")
    async def status(request: Request):
        processor = request.app.state.processor
        accounts = []
        for sub_id, cfg in processor.subs.items():
            adapter = processor.adapter(cfg)
            positions = await adapter.positions()
            state = await adapter.account_state()
            async with processor.session_factory() as session:
                day, risk = await processor._risk(session, cfg)
            accounts.append({
                "subscription_id": sub_id,
                "executor": cfg.executor,
                "confirm_mode": cfg.confirm_mode,
                "equity_usd": state.equity_usd,
                "day_pnl_pct": round(risk.day_pnl_pct, 2),
                "drawdown_pct": round(risk.drawdown_pct, 2),
                "paused": day.paused,
                "pause_reason": day.pause_reason,
                "positions": [{"symbol": p.symbol, "side": p.side, "qty": p.qty}
                              for p in positions],
            })
        async with processor.session_factory() as session:
            decisions = (await session.execute(
                select(Decision).order_by(Decision.id.desc()).limit(20)
            )).scalars().all()
        return {
            "accounts": accounts,
            "recent_decisions": [{"signal_id": d.signal_id, "status": d.status.value,
                                  "action": d.action, "reason": d.reason}
                                 for d in decisions],
        }

    app.router.lifespan_context = lifespan
    return app


async def _expire_loop(processor: Processor) -> None:
    while True:
        try:
            await processor.expire_stale()
        except Exception:
            log.exception("expire pass failed")
        await asyncio.sleep(60)
