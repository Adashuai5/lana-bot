"""Market regime helpers shared by collect/decide/execute."""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass

import httpx
from loguru import logger

BASE_URL = "https://fapi.binance.com"
TIMEOUT = httpx.Timeout(10.0)


@dataclass
class GateResult:
    state: str  # allow | reduce | block
    size_multiplier: float
    reasons: list[str]


def _fetch_klines(symbol: str, interval: str, limit: int) -> list[tuple[int, float]]:
    r = httpx.get(
        f"{BASE_URL}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out: list[tuple[int, float]] = []
    for row in r.json():
        out.append((int(row[0]), float(row[4])))  # open time, close
    return out


def _trend_direction(closes: list[float], drift_up: float, drift_down: float) -> tuple[str, float]:
    if len(closes) < 2 or closes[0] <= 0:
        return "flat", 0.0
    change_pct = (closes[-1] - closes[0]) / closes[0]
    if change_pct >= drift_up:
        return "up", change_pct
    if change_pct <= -abs(drift_down):
        return "down", change_pct
    return "flat", change_pct


def _realized_vol(series: list[float]) -> float:
    if len(series) < 2:
        return 0.0
    rets = []
    for i in range(1, len(series)):
        if series[i - 1] <= 0 or series[i] <= 0:
            continue
        rets.append(math.log(series[i] / series[i - 1]))
    if len(rets) < 2:
        return 0.0
    return statistics.pstdev(rets)


def _percentile_rank(samples: list[float], value: float) -> float:
    if not samples:
        return 0.0
    le = sum(1 for x in samples if x <= value)
    return le / len(samples)


def compute_market_regime(tickers_24h: list | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    rcfg = cfg.get("regime", {})
    enabled = bool(rcfg.get("enabled", False))
    if not enabled:
        return {
            "enabled": False,
            "generated_at_ms": int(time.time() * 1000),
            "gate": "allow",
            "reasons": ["regime disabled"],
        }

    trend_bars_4h = int(rcfg.get("trend_lookback_bars_4h", 30))
    trend_up = float(rcfg.get("trend_drift_up_pct_4h", 0.02))
    trend_down = float(rcfg.get("trend_drift_down_pct_4h", 0.02))
    vol_window_hours = int(rcfg.get("vol_window_hours", 24))
    vol_history_hours = int(rcfg.get("vol_history_hours", 24 * 14))

    btc = _fetch_klines("BTCUSDT", "4h", trend_bars_4h)
    eth = _fetch_klines("ETHUSDT", "4h", trend_bars_4h)
    btc_trend, btc_change = _trend_direction([x[1] for x in btc], trend_up, trend_down)
    eth_trend, eth_change = _trend_direction([x[1] for x in eth], trend_up, trend_down)

    up = 0
    total = 0
    if tickers_24h is not None:
        for t in tickers_24h:
            total += 1
            if float(t.price_change_pct) > 0:
                up += 1
    breadth = (up / total) if total else 0.0

    # 1h realized volatility percentile from BTC
    btc_1h = _fetch_klines(
        "BTCUSDT",
        "1h",
        max(vol_history_hours + vol_window_hours + 5, 50),
    )
    closes = [x[1] for x in btc_1h]
    current_vol = _realized_vol(closes[-vol_window_hours:])
    vol_samples: list[float] = []
    for i in range(vol_window_hours, len(closes)):
        window = closes[i - vol_window_hours:i]
        vol_samples.append(_realized_vol(window))
    vol_quantile = _percentile_rank(vol_samples, current_vol)

    regime = {
        "enabled": True,
        "generated_at_ms": int(time.time() * 1000),
        "trend_4h": {
            "btc": {"direction": btc_trend, "change_pct": round(btc_change * 100, 3)},
            "eth": {"direction": eth_trend, "change_pct": round(eth_change * 100, 3)},
        },
        "breadth": {"up_ratio": round(breadth, 4), "sample_size": total},
        "volatility": {
            "metric": "btc_1h_realized_vol",
            "window_hours": vol_window_hours,
            "value": round(current_vol, 8),
            "quantile": round(vol_quantile, 4),
        },
    }
    gate = decide_regime_gate(regime, cfg)
    regime["gate"] = gate.state
    regime["size_multiplier"] = gate.size_multiplier
    regime["reasons"] = gate.reasons
    return regime


def decide_regime_gate(regime: dict, cfg: dict | None = None) -> GateResult:
    cfg = cfg or {}
    rcfg = cfg.get("regime", {})
    if not rcfg.get("enabled", False):
        return GateResult("allow", 1.0, ["regime disabled"])

    breadth = float(regime.get("breadth", {}).get("up_ratio", 0.0))
    vol_q = float(regime.get("volatility", {}).get("quantile", 0.0))
    btc_t = regime.get("trend_4h", {}).get("btc", {}).get("direction", "flat")
    eth_t = regime.get("trend_4h", {}).get("eth", {}).get("direction", "flat")

    breadth_allow_min = float(rcfg.get("breadth_allow_min", 0.52))
    breadth_reduce_min = float(rcfg.get("breadth_reduce_min", 0.45))
    vol_allow_q_max = float(rcfg.get("vol_allow_quantile_max", 0.75))
    vol_reduce_q_max = float(rcfg.get("vol_reduce_quantile_max", 0.90))
    reduce_size_multiplier = float(rcfg.get("reduce_size_multiplier", 0.5))

    trend_score = int(btc_t == "up") + int(eth_t == "up") - int(btc_t == "down") - int(eth_t == "down")
    reasons = [
        f"trend btc={btc_t} eth={eth_t}",
        f"breadth={breadth:.3f}",
        f"vol_q={vol_q:.3f}",
    ]

    if trend_score <= -1 or breadth < breadth_reduce_min or vol_q > vol_reduce_q_max:
        return GateResult("block", 0.0, reasons)
    if trend_score <= 0 or breadth < breadth_allow_min or vol_q > vol_allow_q_max:
        return GateResult("reduce", reduce_size_multiplier, reasons)
    return GateResult("allow", 1.0, reasons)


def safe_compute_market_regime(tickers_24h: list | None = None, cfg: dict | None = None) -> dict:
    try:
        return compute_market_regime(tickers_24h=tickers_24h, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("market regime compute failed: {}", e)
        return {
            "enabled": bool((cfg or {}).get("regime", {}).get("enabled", False)),
            "generated_at_ms": int(time.time() * 1000),
            "gate": "allow",
            "size_multiplier": 1.0,
            "reasons": [f"regime unavailable: {e}"],
        }
