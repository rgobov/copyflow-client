from __future__ import annotations

from copyflow_client.config import ClientSettings, SubConfig
from copyflow_core.executors.base import ExecutionAdapter, ExecutionError
from copyflow_core.executors.binance import BinanceExecutor
from copyflow_core.executors.hyperliquid import HyperliquidExecutor
from copyflow_core.executors.paper import PaperConfig, PaperExecutor


def build_adapter(cfg: SubConfig, settings: ClientSettings) -> ExecutionAdapter:
    if cfg.executor == "paper":
        provider = None
        if settings.use_marketdata:
            from copyflow_core.instruments import InstrumentRegistry
            from copyflow_core.marketdata import MultiMarketData

            registry = InstrumentRegistry(verify_tls=settings.market_tls_verify)
            md = MultiMarketData(registry=registry, verify_tls=settings.market_tls_verify)

            async def provider(symbol: str) -> float | None:
                return await md.price_by_symbol(symbol)

        return PaperExecutor(PaperConfig(starting_equity_usd=cfg.equity_usd), price_provider=provider)
    if cfg.executor == "binance":
        return BinanceExecutor(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            testnet=settings.binance_testnet,
        )
    if cfg.executor == "hyperliquid":
        return HyperliquidExecutor(
            api_url=settings.hl_api_url,
            agent_private_key=settings.hl_agent_private_key,
            agent_address=settings.hl_agent_address,
        )
    raise ExecutionError(f"unknown executor {cfg.executor}")
