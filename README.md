# copyflow-client

Copy-trading client app: receives **signed signals** from a copyflow server,
applies **its own** risk limits and executes through **its own** exchange keys.
The server never sees your keys and never trades for you — it only watches
leaders and pushes facts. You decide.

> ⚠️ **Disclaimer**: trading crypto (especially leveraged futures) can lose
> money quickly. This software is a tool, not financial advice. Past
> performance of any leader does not guarantee future results.
> Licensed under **AGPL-3.0**.

```
copyflow server ──(signed webhook)──> copyflow-client ──> your exchange account
   watches leaders                    verifies HMAC
   pushes signals                     applies YOUR limits
   pushes risk events                 executes (auto / 1-click / manual)
```

## What it does

- **`POST /hook`** — receives signals: HMAC-SHA256 signature (`X-Copyflow-Signature`),
  anti-replay (timestamp freshness), idempotency by signal id
- **Own risk engine** (your money, your rules, `client.json`):
  - sizing: `equity_frac` / `fixed_usd` / `proportional`, cap `max_notional_usd`
  - leverage cap, max positions, blacklists, price-deviation guard, min notional
  - daily loss limit, max drawdown from peak — with background monitoring
    every 10s while positions are open
  - `on_breach`: `pause` | `pause_and_close` | `notify_only`
- **Execution modes** (`confirm_mode`):
  - `auto` — execute immediately
  - `confirm` — Telegram inline buttons Execute / Skip (TTL 10 min)
  - `manual` — notification only, you trade by hand (Pulse-style)
- **Executors**: `paper` (virtual account), `hyperliquid` (agent wallet without
  withdraw rights), `binance` (USD-M futures, trade-only keys)
- **Local ledger**: `client.db` (SQLite) — orders, fills, decisions, risk days
- `GET /status` — equity, positions, day pnl, drawdown, decisions
- `POST /resume` — clear a risk pause

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env            # set CF_WEBHOOK_SECRET (from your server subscription)
cp client.json.example client.json
.venv/bin/uvicorn client.main:app --port 9000

# e2e check without a server: send yourself a signed signal
.venv/bin/python scripts/demo_signal.py
```

For live trading: `pip install -e '.[live]'` and fill executor keys in `.env`.
Keys must be **trade-only** (no withdrawal permission); for Hyperliquid use an
**agent wallet** without withdraw rights.

## Subscribe on a copyflow server

Create a subscription with `delivery: "webhook"`, point it at this app:

```
webhook_url: https://your-host:9000/hook
webhook_secret: <same as CF_WEBHOOK_SECRET>
```

Payload contract (signed JSON): `type`, `signal_id`, `leader_id`,
`leader_name`, `leader_equity_usd`, `subscription_id`,
`signal` {instrument, action: open|increase|reduce|flip|close, side,
size_frac, size_abs_usd, ref_price, take_profit, stop_loss},
`risk` {daily_loss_limit_pct, max_drawdown_pct, peak_equity}.

## Development

```bash
pytest -v          # unit tests (core sizing, paper exec, client flows)
ruff check .
```

License: [AGPL-3.0](LICENSE).
