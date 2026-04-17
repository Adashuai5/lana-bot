"""Exchange clients + factory."""
from __future__ import annotations

from lana_bot.config import exchange_keys, strategy
from lana_bot.execution.base import ExchangeClient
from lana_bot.execution.simulator import Simulator


def get_client() -> ExchangeClient:
    cfg = strategy()
    if not cfg.get("live_trading"):
        return Simulator()

    ex = cfg["exchange"]
    keys = exchange_keys().get(ex, {})
    if ex == "binance":
        from lana_bot.execution.binance import BinanceFutures
        return BinanceFutures(keys["api_key"], keys["api_secret"])
    if ex == "gate":
        from lana_bot.execution.gate import GateFutures
        return GateFutures(keys["api_key"], keys["api_secret"])
    raise ValueError(f"unknown exchange: {ex}")
