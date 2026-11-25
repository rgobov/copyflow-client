from __future__ import annotations

import time

import httpx

from copyflow_client.config import ClientSettings, SubConfig
from copyflow_client.receiver import create_app
from copyflow_core.webhooks import sign


def make_settings(tmp_path, secret="s3cret") -> ClientSettings:
    return ClientSettings(
        webhook_secret=secret,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/client.db",
        max_signal_age_sec=300,
    )


def make_subs(**kw) -> dict[int, SubConfig]:
    cfg = SubConfig(subscription_id=1, **kw)
    return {1: cfg}


def make_app(tmp_path, subs=None, secret="s3cret"):
    settings = make_settings(tmp_path, secret)
    subs = subs or make_subs()
    app = create_app(settings, subs)
    return app, settings, subs


def signed_post_payload(payload: dict, secret: str = "s3cret") -> dict:
    import json

    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    return {
        "content": body,
        "headers": {
            "Content-Type": "application/json",
            "X-Copyflow-Signature": sign(secret, body, ts),
            "X-Copyflow-Timestamp": ts,
        },
    }


def signal_payload(sub_id: int = 1, action: str = "open", side: str = "long",
                   frac: float = 0.5, signal_id: str = "sig-1") -> dict:
    return {
        "type": "signal",
        "signal_id": signal_id,
        "leader_id": 7,
        "leader_name": "VBO ORB",
        "leader_equity_usd": 10_000.0,
        "subscription_id": sub_id,
        "signal": {
            "id": signal_id,
            "leader_id": 7,
            "instrument": {"venue": "paper", "symbol": "BTC"},
            "action": action, "side": side, "size_frac": frac,
            "ref_price": 100.0, "ts": "2026-11-24T20:00:00Z",
        },
        "risk": {"daily_loss_limit_pct": 5.0, "max_drawdown_pct": 20.0},
    }


async def test_auto_execution_flow(tmp_path):
    app, settings, subs = make_app(tmp_path, subs=make_subs(confirm_mode="auto"))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://c") as c:
            r = await c.post("/hook", **signed_post_payload(signal_payload()))
            assert r.status_code == 200
            assert r.json()["result"]["status"] == "executed"

            processor = app.state.processor
            positions = await processor.adapter(subs[1]).positions()
            assert len(positions) == 1
            assert positions[0].side == "long"

            # close signal
            r = await c.post("/hook", **signed_post_payload(
                signal_payload(action="close", side="long", frac=1.0, signal_id="sig-2")))
            assert r.json()["result"]["status"] == "executed"
            assert await processor.adapter(subs[1]).positions() == []


async def test_bad_signature_rejected(tmp_path):
    app, _, _ = make_app(tmp_path, secret="s3cret")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://c") as c:
            payload = signed_post_payload(signal_payload(), secret="wrong")
            r = await c.post("/hook", **payload)
            assert r.status_code == 403


async def test_stale_timestamp_rejected(tmp_path):
    app, settings, _ = make_app(tmp_path, subs=make_subs(confirm_mode="auto"))
    import json

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://c") as c:
            body = json.dumps(signal_payload()).encode()
            ts = str(int(time.time()) - settings.max_signal_age_sec - 10)
            r = await c.post("/hook", content=body, headers={
                "X-Copyflow-Signature": sign("s3cret", body, ts),
                "X-Copyflow-Timestamp": ts,
            })
            assert r.status_code == 403


async def test_duplicate_signal_idempotent(tmp_path):
    app, _, _ = make_app(tmp_path, subs=make_subs(confirm_mode="auto"))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://c") as c:
            r1 = await c.post("/hook", **signed_post_payload(signal_payload()))
            r2 = await c.post("/hook", **signed_post_payload(signal_payload()))
            assert r1.json()["result"]["status"] == "executed"
            assert "duplicate" in r2.json()["note"]

            from sqlalchemy import select

            from copyflow_client.models import Order

            async with app.state.processor.session_factory() as s:
                orders = (await s.execute(select(Order))).scalars().all()
                assert len(orders) == 1


async def test_client_daily_loss_pauses_and_flattens(tmp_path):
    app, _, subs = make_app(tmp_path, subs=make_subs(confirm_mode="auto"))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://c") as c:
            await c.post("/hook", **signed_post_payload(signal_payload()))
            processor = app.state.processor
            adapter = processor.adapter(subs[1])
            assert len(await adapter.positions()) == 1

            # simulate loss: price crash inside the paper account
            async def cheap(symbol):
                return 80.0

            adapter._price_provider = cheap
            r = await c.post("/hook", **signed_post_payload(
                signal_payload(action="open", frac=0.1, signal_id="sig-3")))
            assert r.json()["result"]["status"] == "skipped"
            assert "risk" in r.json()["result"]["reason"]

            # pause_and_close flattened the position
            assert await adapter.positions() == []

            from sqlalchemy import select

            from copyflow_client.models import RiskDay

            async with processor.session_factory() as s:
                day = (await s.execute(select(RiskDay))).scalar_one()
                assert day.paused
                assert "risk breach" in day.pause_reason

            # manual resume endpoint
            r = await c.post("/resume")
            assert r.json()["resumed"] == [1]


async def test_confirm_mode_awaits_user(tmp_path):
    subs = make_subs(confirm_mode="confirm")
    app, _, _ = make_app(tmp_path, subs=subs)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://c") as c:
            r = await c.post("/hook", **signed_post_payload(signal_payload()))
            assert r.json()["result"]["status"] == "awaiting_confirm"

            processor = app.state.processor
            # nothing executed yet
            assert await processor.adapter(subs[1]).positions() == []

            # user confirms via button
            res = await processor.execute_confirmed("sig-1", accept=True)
            assert res["status"] == "executed"
            assert len(await processor.adapter(subs[1]).positions()) == 1

            # re-confirm -> stale
            res = await processor.execute_confirmed("sig-1", accept=True)
            assert res["status"] == "stale"


async def test_confirm_mode_decline(tmp_path):
    subs = make_subs(confirm_mode="confirm")
    app, _, _ = make_app(tmp_path, subs=subs)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://c") as c:
            await c.post("/hook", **signed_post_payload(signal_payload(signal_id="sig-9")))
            processor = app.state.processor
            res = await processor.execute_confirmed("sig-9", accept=False)
            assert res["status"] == "declined"
            assert await processor.adapter(subs[1]).positions() == []


async def test_unknown_subscription_ignored(tmp_path):
    app, _, _ = make_app(tmp_path, subs=make_subs())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://c") as c:
            r = await c.post("/hook", **signed_post_payload(signal_payload(sub_id=42)))
            assert r.json()["result"]["reason"] == "no local config"
