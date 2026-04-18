"""Gate.io futures live client. STUB — fallback if Binance futures unavailable.

Docs: https://www.gate.io/docs/developers/apiv4/#futures
"""
from __future__ import annotations

from lana_bot.execution.base import FillResult


class GateFutures:
    name = "gate"
    is_stub = True

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret

    def get_mark_price(self, symbol: str) -> float:
        raise NotImplementedError("Gate live client not yet implemented")

    def open_long(self, symbol: str, size_usdt: float, leverage: int) -> FillResult:
        raise NotImplementedError("Gate live client not yet implemented")

    def close(self, symbol: str) -> FillResult:
        raise NotImplementedError("Gate live client not yet implemented")
