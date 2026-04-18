"""Binance USD-M futures live client. STUB — implement before flipping live_trading=true.

Signing for POST /fapi/v1/order is HMAC-SHA256 over querystring. See:
https://developers.binance.com/docs/derivatives/usds-margined-futures
"""
from __future__ import annotations

from lana_bot.execution.base import FillResult


class BinanceFutures:
    name = "binance"

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret

    def get_mark_price(self, symbol: str) -> float:
        from lana_bot.data.binance_futures import fetch_mark_price
        return fetch_mark_price(symbol)

    def open_long(self, symbol: str, size_usdt: float, leverage: int) -> FillResult:
        raise NotImplementedError("Binance live client not yet implemented")

    def close(self, symbol: str, exit_trigger: str = "signal_decay") -> FillResult:
        raise NotImplementedError("Binance live client not yet implemented")
