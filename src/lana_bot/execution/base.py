"""Abstract exchange client interface. Implementations: binance, gate, simulator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class FillResult:
    symbol: str
    side: str        # "LONG" | "CLOSE"
    price: float
    size_usdt: float
    leverage: int
    ts_ms: int


class ExchangeClient(Protocol):
    name: str

    def get_mark_price(self, symbol: str) -> float: ...

    def open_long(self, symbol: str, size_usdt: float, leverage: int) -> FillResult: ...

    def open_short(self, symbol: str, size_usdt: float, leverage: int) -> FillResult: ...

    def close(self, symbol: str, exit_trigger: str = "signal_decay") -> FillResult: ...
