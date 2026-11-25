from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class SubConfig(BaseModel):
    """Client-owned risk profile for one server subscription id.

    These limits are the client's own; the server never decides here.
    """

    subscription_id: int
    name: str = ""
    executor: str = "paper"  # paper | hyperliquid | binance
    confirm_mode: str = "confirm"  # confirm (1-click default) | auto | manual
    sizing: str = "equity_frac"  # equity_frac | fixed_usd | proportional
    fixed_usd: float = 0.0
    equity_usd: float = 1_000.0  # fallback for paper when live equity unknown
    leverage_cap: float = 2.0
    max_price_deviation_pct: float = 1.5
    slippage_bps: int = 15
    min_notional_usd: float = 10.0
    max_positions: int = 5
    max_notional_usd: float = 500.0
    blacklisted_symbols: list[str] = []
    daily_loss_limit_pct: float = 5.0
    max_drawdown_pct: float = 20.0
    on_breach: str = "pause_and_close"  # pause | pause_and_close | notify_only


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix="CF_")

    listen_host: str = "127.0.0.1"
    listen_port: int = 9000
    database_url: str = "sqlite+aiosqlite:///./client.db"
    webhook_secret: str = ""
    config_file: str = "client.json"
    max_signal_age_sec: int = 300
    risk_check_interval_sec: float = 10.0
    confirm_ttl_sec: int = 600
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True
    hl_agent_private_key: str = ""
    hl_agent_address: str = ""
    hl_api_url: str = "https://api.hyperliquid.xyz"


@lru_cache
def get_settings() -> ClientSettings:
    return ClientSettings()


def load_subscriptions(path: str) -> dict[int, SubConfig]:
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {int(k): SubConfig(subscription_id=int(k), **v) for k, v in data.get("subscriptions", {}).items()}
