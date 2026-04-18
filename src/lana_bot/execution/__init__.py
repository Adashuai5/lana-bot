"""Exchange clients + factory."""
from __future__ import annotations

from lana_bot.config import exchange_keys, strategy
from lana_bot.execution.base import ExchangeClient
from lana_bot.execution.simulator import Simulator


def _assert_live_client_ready(ex: str, client: ExchangeClient) -> None:
    if not getattr(client, "is_stub", False):
        return
    raise RuntimeError(
        "live_trading=true but selected exchange client is still a stub: "
        f"exchange={ex}, client={client.__class__.__name__}. "
        "Fix: keep live_trading=false, or implement/replace this exchange client "
        "with signed order placement, cancel-order flow, position sync, and retries."
    )


def get_client() -> ExchangeClient:
    cfg = strategy()
    if not cfg.get("live_trading"):
        return Simulator()

    ex = cfg["exchange"]
    keys = exchange_keys().get(ex, {})
    if ex == "binance":
        from lana_bot.execution.binance import BinanceFutures
        client = BinanceFutures(keys["api_key"], keys["api_secret"])
        _assert_live_client_ready(ex, client)
        return client
    if ex == "gate":
        from lana_bot.execution.gate import GateFutures
        client = GateFutures(keys["api_key"], keys["api_secret"])
        _assert_live_client_ready(ex, client)
        return client
    raise ValueError(f"unknown exchange: {ex}")
