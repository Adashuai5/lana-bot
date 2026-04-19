"""Binance USD-M futures public REST helpers.

No auth required for the endpoints used here — read-only market data.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

BASE_URL = "https://fapi.binance.com"
TIMEOUT = httpx.Timeout(10.0)


@dataclass
class Ticker24h:
    symbol: str
    last_price: float
    price_change_pct: float
    quote_volume: float
    high_price: float = 0.0
    low_price: float = 0.0

    @property
    def gain_from_low_pct(self) -> float:
        """How much price has risen from the 24h low. Catches pumps invisible to 24h net change."""
        if self.low_price <= 0:
            return 0.0
        return (self.last_price - self.low_price) / self.low_price * 100


@dataclass
class OiPoint:
    symbol: str
    open_interest: float
    timestamp_ms: int


def fetch_all_24h_tickers() -> list[Ticker24h]:
    r = httpx.get(f"{BASE_URL}/fapi/v1/ticker/24hr", timeout=TIMEOUT)
    r.raise_for_status()
    out: list[Ticker24h] = []
    for item in r.json():
        symbol = item["symbol"]
        if not symbol.endswith("USDT"):
            continue
        out.append(
            Ticker24h(
                symbol=symbol,
                last_price=float(item["lastPrice"]),
                price_change_pct=float(item["priceChangePercent"]),
                quote_volume=float(item["quoteVolume"]),
                high_price=float(item.get("highPrice", 0) or 0),
                low_price=float(item.get("lowPrice", 0) or 0),
            )
        )
    return out


def fetch_oi_history(symbol: str, period: str = "5m", limit: int = 13) -> list[OiPoint]:
    """Open interest history. Default: 13 points of 5m (~1h window)."""
    r = httpx.get(
        f"{BASE_URL}/futures/data/openInterestHist",
        params={"symbol": symbol, "period": period, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return [
        OiPoint(
            symbol=p["symbol"],
            open_interest=float(p["sumOpenInterest"]),
            timestamp_ms=int(p["timestamp"]),
        )
        for p in r.json()
    ]


def fetch_mark_price(symbol: str) -> float:
    r = httpx.get(
        f"{BASE_URL}/fapi/v1/premiumIndex",
        params={"symbol": symbol},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return float(r.json()["markPrice"])


@dataclass
class KlineBar:
    high: float
    low: float
    close: float


def fetch_klines(symbol: str, interval: str = "1h", limit: int = 5) -> list[KlineBar]:
    """Fetch OHLC klines. Returns up to `limit` bars."""
    r = httpx.get(
        f"{BASE_URL}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return [KlineBar(high=float(k[2]), low=float(k[3]), close=float(k[4])) for k in r.json()]


def oi_change_pct(points: list[OiPoint]) -> float:
    """Percent change from first to last OI sample."""
    if len(points) < 2 or points[0].open_interest == 0:
        return 0.0
    return (points[-1].open_interest - points[0].open_interest) / points[0].open_interest * 100
