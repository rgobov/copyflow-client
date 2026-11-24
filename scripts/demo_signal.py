"""Send a signed fake signal to a running copyflow-client (localhost:9000).

Lets you test the client without a copyflow server.
Run: .venv/bin/python scripts/demo_signal.py
"""
import asyncio
import json
import os
import time
import uuid

import httpx

from copyflow_core.webhooks import sign

API = "http://127.0.0.1:9000/hook"
SECRET = os.environ.get("CF_WEBHOOK_SECRET", "")


async def main() -> None:
    signal_id = uuid.uuid4().hex
    payload = {
        "type": "signal",
        "signal_id": signal_id,
        "leader_id": 1,
        "leader_name": "demo-leader",
        "leader_equity_usd": 10_000.0,
        "subscription_id": 1,
        "signal": {
            "id": signal_id,
            "leader_id": 1,
            "instrument": {"venue": "paper", "symbol": "BTC"},
            "action": "open",
            "side": "long",
            "size_frac": 0.3,
            "ref_price": 95_000.0,
        },
        "risk": {"daily_loss_limit_pct": 5.0, "max_drawdown_pct": 20.0},
    }
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    r = await httpx.AsyncClient(timeout=10).post(
        API, content=body,
        headers={"Content-Type": "application/json",
                 "X-Copyflow-Signature": sign(SECRET, body, ts),
                 "X-Copyflow-Timestamp": ts},
    )
    print(r.status_code, r.json())


if __name__ == "__main__":
    asyncio.run(main())
