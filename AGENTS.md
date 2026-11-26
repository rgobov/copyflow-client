# AGENTS.md — copyflow-client

Инструкции для ИИ-агентов и разработчиков, работающих с этим репозиторием.

## Что это

Клиентское приложение платформы copyflow: принимает **подписанные** сигналы
от copyflow-сервера, применяет **собственные** риск-лимиты пользователя и
исполняет их через **собственные** ключи бирж. Клиент — единственное место,
где принимаются торговые решения; сервер только информирует. Лицензия
**AGPL-3.0**; публичный репозиторий — секреты в git не попадают никогда.

## Структура

```
copyflow_core/          # разделяемое ядро (используется и сервером)
├── models.py           # канонический SignalEvent (venue, action, size_frac, tp/sl)
├── sizing.py           # CopyLimits + compute_size (сайзинг, лимиты, guard'ы)
├── executors/          # ExecutionAdapter + paper / hyperliquid / binance
├── webhooks.py         # sign / verify (HMAC-SHA256 по ts.body)
├── instruments.py      # InstrumentSpec (лоты, валюты, рынки) + MOEX реестр
└── marketdata.py       # MultiMarketData: Binance / MOEX ISS / Yahoo (US+IN) + FX CBR

copyflow_client/
├── config.py           # ClientSettings (CF_*) + загрузка client.json (per-sub лимиты)
├── db.py / models.py   # локальный SQLite: signals, decisions, orders, fills, risk_days
├── risk.py             # клиентский риск: дневной лимит, drawdown, pause/flatten
├── receiver.py         # FastAPI: POST /hook (HMAC+анти-replay), /status, /resume
├── processor.py        # конвейер: сигнал -> риск -> сайзинг -> исполнение/подтверждение
├── executor.py         # сборка адаптера по конфигу подписки
├── monitor.py          # фоновый мониторинг equity (открытые позиции)
├── telegram.py         # пуш + inline-кнопки Execute/Skip
└── main.py             # сборка приложения

tests/                  # 32 теста: sizing, paper, receiver (HMAC/replay/idempotency),
                        # confirm-flow, клиентский риск, marketdata
scripts/demo_signal.py  # отправить себе подписанный сигнал без сервера
```

## Команды

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

.venv/bin/python -m pytest tests -q      # 32 теста
.venv/bin/ruff check .                   # линт

.venv/bin/uvicorn client.main:app --port 9000
.venv/bin/python scripts/demo_signal.py  # CF_WEBHOOK_SECRET из .env
```

## Критичные инварианты (НЕ ЛОМАТЬ)

1. **`POST /hook`**: проверка `X-Copyflow-Signature: sha256=HMAC(secret, ts.body)`
   + анти-replay (timestamp ± `CF_MAX_SIGNAL_AGE_SEC`) + дедуп по `signal_id`.
   Все три проверки обязательны, порядок и отказ 403 не ослаблять.
2. **Клиент решает сам**: сервер присылает факт сигнала; сайзинг, лимиты,
   режимы — из `client.json` пользователя. Серверные «рекомендации» риска
   (`risk` в payload) — входные данные, не приказы.
3. **Аллокация строгая**: `total_capital_usd > 0` && `allocation_pct == 0`
   -> сигнал пропускается (`no allocation`). Effective equity =
   `total_capital * allocation_pct / 100`.
4. **confirm_mode**: `confirm` — сигнал ждёт кнопки (Decision.AWAITING, TTL
   `CF_CONFIRM_TTL_SEC`); `manual` — только уведомление; `auto` — исполнение.
5. **Биржевые ключи** — только локально в `.env`/`client.json`; token HL —
   agent wallet без права вывода; ключи CEX — trade-only.
6. **Идемпотентность** по signal_id + уникальный client_order_id на ордер.

## Конфигурация

- `.env` (префикс `CF_`): `CF_WEBHOOK_SECRET` (общий секрет с сервером),
  `CF_USE_MARKETDATA=1` (мультирыночные цены для paper: BTC/SBER/AAPL/RELIANCE.NS),
  `CF_MARKET_TLS_VERIFY` (false — только для сетей с MITM публичных эндпоинтов),
  ключи исполнителей, Telegram
- `client.json` (gitignored, есть `.example`): per-subscription
  `executor / confirm_mode / sizing / equity_usd / allocation_pct / leverage_cap /
  daily_loss_limit_pct / max_drawdown_pct / on_breach (pause|pause_and_close|notify_only)`

## Грабли

- **Русский CA Минцифры** отсутствует в certifi -> MOEX ISS и CBR дают
  `self-signed certificate in chain` из Python. Варианты:
  `CF_MARKET_TLS_VERIFY=false` (публичные котировки допустимо) или системный
  CA-бандл (`/etc/ssl/certs/ca-certificates.crt` на Debian 12/Ubuntu 24.04+)
- `async_sessionmaker` — вызывать: `async with sf() as session`
- MOEX отдаёт валюту legacy-кодом `SUR` — нормализуется в `RUB`
- `'app'` недоступен из этого репо: ядро — только `copyflow_core*`

## Публикация

AGPL-3.0: любые модификации, используемые в сетевом сервисе, обязаны
открываться под той же лицензией. Не коммитить `client.json`, `.env`,
`*.db` — там секреты и ключи.
