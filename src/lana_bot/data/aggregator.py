"""Combine ticker gains + OI change into a ranked candidate list.

Outputs a dict ready to be JSON-dumped for Claude to consume.
"""
from __future__ import annotations

import math
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass

from loguru import logger

from lana_bot.config import strategy
from lana_bot.data.binance_futures import (
    fetch_all_24h_tickers,
    fetch_klines,
    fetch_oi_history,
    oi_change_pct,
)
from lana_bot.data.market_regime import safe_compute_market_regime


@dataclass
class Candidate:
    symbol: str
    last_price: float
    price_change_pct: float
    quote_volume_usdt: float
    oi_change_1h_pct: float
    oi_change_4h_pct: float
    trend_score: float
    oi_score: float
    liquidity_score: float
    score: float
    square_mentions: int = 0
    side: str = "LONG"  # "LONG" | "SHORT"
    pct_from_4h_high: float = 0.0   # how far below 5h peak (higher = more pullback = safer entry)
    atr_pct: float = 0.0            # avg true range as % of price over last 5h (volatility proxy)
    gain_from_low_pct: float = 0.0  # gain from 24h low; high value = active pump even if 24h net is negative


def _slope(points: list[float]) -> float:
    if len(points) < 2:
        return 0.0
    return (points[-1] - points[0]) / (len(points) - 1)


def _continuity_ratio(values: list[float]) -> float:
    if len(values) < 3:
        return 0.0
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    overall = values[-1] - values[0]
    if overall == 0:
        return 0.0
    sign = 1 if overall > 0 else -1
    aligned = sum(1 for d in deltas if d * sign > 0)
    return aligned / len(deltas)


def _max_counter_run(values: list[float]) -> int:
    """Max consecutive bars moving against the overall direction."""
    if len(values) < 3:
        return 0
    overall = values[-1] - values[0]
    if overall == 0:
        return 0
    sign = 1 if overall > 0 else -1
    max_run = cur = 0
    for i in range(1, len(values)):
        if (values[i] - values[i - 1]) * sign <= 0:
            cur += 1
            max_run = max(max_run, cur)
        else:
            cur = 0
    return max_run


def _step_volatility_pct(values: list[float]) -> float:
    if len(values) < 3:
        return 0.0
    step_returns = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev == 0:
            continue
        step_returns.append((values[i] - prev) / prev * 100)
    if len(step_returns) < 2:
        return 0.0
    return statistics.pstdev(step_returns)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = (len(ordered) - 1) * q
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    ratio = idx - lo
    return ordered[lo] * (1 - ratio) + ordered[hi] * ratio


def _winsorized(values: list[float], low_q: float = 0.1, high_q: float = 0.9) -> list[float]:
    if not values:
        return []
    low = _quantile(values, low_q)
    high = _quantile(values, high_q)
    return [max(low, min(v, high)) for v in values]


def _zscore(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    if std == 0:
        return [0.0] * len(values)
    return [(v - mean) / std for v in values]


def _bounded_score(z: float) -> float:
    # Segmented/bounded mapping to reduce outlier impact.
    return (math.tanh(z) + 1) * 50


def build_candidates(square_mentions: dict[str, int] | None = None) -> dict:
    cfg = strategy()
    filters = cfg["filters"]
    square_mentions = square_mentions or {}

    tickers = fetch_all_24h_tickers()
    logger.info("fetched {} USDT-perp tickers", len(tickers))
    regime = safe_compute_market_regime(tickers_24h=tickers, cfg=cfg)

    filter_reason_stats: Counter[str] = Counter()
    min_short_gain = float(filters.get("min_short_gain_pct", 0))

    # Stage 1: hard filters.
    # Primary path: 24h net change >= threshold (clean pump).
    # Secondary path: gain_from_low >= min_short_gain (pump from day's bottom,
    #   invisible to 24h net change when 24h base price was already elevated).
    stage1_passed = []
    for t in tickers:
        if t.quote_volume < filters["min_24h_volume_usdt"]:
            filter_reason_stats["stage1_low_volume"] += 1
            continue
        passes_24h = t.price_change_pct >= filters["min_24h_change_pct"]
        passes_low = min_short_gain > 0 and t.gain_from_low_pct >= min_short_gain
        if not passes_24h and not passes_low:
            filter_reason_stats["stage1_low_24h_change"] += 1
            continue
        stage1_passed.append(t)

    # Sort: prefer 24h movers first, then gain_from_low candidates
    stage1_passed.sort(
        key=lambda t: max(t.price_change_pct, t.gain_from_low_pct),
        reverse=True,
    )
    prefiltered = stage1_passed[: filters["top_n_candidates"] * 2]
    logger.info("stage1 filtered to {}", len(prefiltered))

    min_oi_4h = float(filters.get("min_oi_change_4h_pct", 0))
    max_gap_1h = int(filters.get("max_oi_gap_bars_1h", 2))   # max consecutive counter bars in 1h window
    max_gap_4h = int(filters.get("max_oi_gap_bars_4h", 3))   # max consecutive counter bars in 4h window
    max_step_vol = float(filters.get("max_oi_step_volatility_pct", 1.5))
    min_pullback_pct = float(filters.get("min_pullback_from_high_pct", 0))

    # Stage 2: OI stability constraints + pullback filter.
    stage2_rows: list[dict[str, float | str | int]] = []
    for t in prefiltered:
        try:
            oi_points = fetch_oi_history(t.symbol, period="5m", limit=49)  # ~4h
        except Exception as e:  # noqa: BLE001
            logger.warning("oi fetch failed for {}: {}", t.symbol, e)
            filter_reason_stats["stage2_oi_fetch_failed"] += 1
            continue

        oi_values = [p.open_interest for p in oi_points]
        oi_1h_points = oi_points[-13:] if len(oi_points) >= 13 else oi_points
        oi_1h_values = [p.open_interest for p in oi_1h_points]

        oi_pct_1h = oi_change_pct(oi_1h_points)
        oi_pct_4h = oi_change_pct(oi_points)
        step_volatility = _step_volatility_pct(oi_values)
        gap_1h = _max_counter_run(oi_1h_values)
        gap_4h = _max_counter_run(oi_values)

        # Kline-based metrics: pullback from peak + ATR (fetched first to allow dynamic OI threshold)
        pct_from_4h_high = 0.0
        atr_pct = 0.0
        try:
            klines = fetch_klines(t.symbol, interval="1h", limit=5)
            if klines:
                peak = max(k.high for k in klines)
                pct_from_4h_high = (peak - t.last_price) / peak * 100 if peak > 0 else 0.0
                atr_pct = statistics.fmean(
                    (k.high - k.low) / k.close * 100 for k in klines if k.close > 0
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("kline fetch failed for {}: {}", t.symbol, e)

        # FOMO path: sustained pump (gain_from_low >= 50%) with tightening volatility (ATR < 15%)
        # indicates a strong directional trend rather than noise — relax OI threshold to 2%.
        fomo_pump = t.gain_from_low_pct >= float(filters.get("fomo_gain_from_low_pct", 50.0)) and atr_pct < float(filters.get("fomo_atr_max_pct", 15.0))
        oi_1h_threshold = float(filters.get("fomo_oi_min_pct", 2.0)) if fomo_pump else filters["min_oi_change_1h_pct"]

        stage2_failed = False
        if oi_pct_1h < oi_1h_threshold:
            filter_reason_stats["stage2_low_oi_change_1h"] += 1
            stage2_failed = True
        if oi_pct_4h < min_oi_4h:
            filter_reason_stats["stage2_low_oi_change_4h"] += 1
            stage2_failed = True
        if gap_1h > max_gap_1h:
            filter_reason_stats["stage2_low_continuity_1h"] += 1
            stage2_failed = True
        if gap_4h > max_gap_4h:
            filter_reason_stats["stage2_low_continuity_4h"] += 1
            stage2_failed = True
        if step_volatility > max_step_vol:
            filter_reason_stats["stage2_high_oi_volatility"] += 1
            stage2_failed = True

        if min_pullback_pct > 0 and pct_from_4h_high < min_pullback_pct:
            filter_reason_stats["stage2_at_peak"] += 1
            stage2_failed = True

        if stage2_failed:
            continue

        # Use the stronger of 24h change or gain_from_low as the trend signal.
        # This prevents gain_from_low candidates from being buried by a negative price_change_pct.
        effective_gain = max(t.price_change_pct, t.gain_from_low_pct)
        stage2_rows.append(
            {
                "symbol": t.symbol,
                "last_price": t.last_price,
                "price_change_pct": t.price_change_pct,
                "gain_from_low_pct": round(t.gain_from_low_pct, 2),
                "effective_gain_pct": round(effective_gain, 2),
                "quote_volume_usdt": t.quote_volume,
                "oi_change_1h_pct": oi_pct_1h,
                "oi_change_4h_pct": oi_pct_4h,
                "square_mentions": square_mentions.get(t.symbol, 0),
                "pct_from_4h_high": round(pct_from_4h_high, 2),
                "atr_pct": round(atr_pct, 2),
            }
        )

    logger.info("stage2 filtered to {}", len(stage2_rows))

    candidates: list[Candidate] = []
    if stage2_rows:
        trend_raw = [float(r["effective_gain_pct"]) for r in stage2_rows]
        oi_raw = [float(r["oi_change_1h_pct"]) * 0.6 + float(r["oi_change_4h_pct"]) * 0.4 for r in stage2_rows]
        liquidity_raw = [math.log10(max(float(r["quote_volume_usdt"]), 1.0)) for r in stage2_rows]

        trend_z = _zscore(_winsorized(trend_raw))
        oi_z = _zscore(_winsorized(oi_raw))
        liquidity_z = _zscore(_winsorized(liquidity_raw))

        for i, row in enumerate(stage2_rows):
            trend_score = _bounded_score(trend_z[i])
            oi_score = _bounded_score(oi_z[i])
            liquidity_score = _bounded_score(liquidity_z[i])
            mentions = int(row["square_mentions"])
            mention_bonus = min(10.0, mentions * 2.0)
            score = trend_score * 0.4 + oi_score * 0.4 + liquidity_score * 0.2 + mention_bonus

            candidates.append(
                Candidate(
                    symbol=str(row["symbol"]),
                    last_price=float(row["last_price"]),
                    price_change_pct=float(row["price_change_pct"]),
                    quote_volume_usdt=float(row["quote_volume_usdt"]),
                    oi_change_1h_pct=float(row["oi_change_1h_pct"]),
                    oi_change_4h_pct=float(row["oi_change_4h_pct"]),
                    trend_score=trend_score,
                    oi_score=oi_score,
                    liquidity_score=liquidity_score,
                    score=score,
                    square_mentions=mentions,
                    pct_from_4h_high=float(row.get("pct_from_4h_high", 0.0)),
                    atr_pct=float(row.get("atr_pct", 0.0)),
                    gain_from_low_pct=float(row.get("gain_from_low_pct", 0.0)),
                )
            )

    for c in candidates:
        if c.score <= 0:
            filter_reason_stats["stage2_non_positive_score"] += 1


    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[: filters["top_n_candidates"]]

    # Short candidates: squeeze fades (price up sharply + OI dropping)
    short_cfg = cfg.get("short_filters", {})
    short_candidates: list[Candidate] = []
    if short_cfg.get("enabled", False):
        min_squeeze_pct = float(short_cfg.get("min_squeeze_pct", 30.0))
        min_volume = float(short_cfg.get("min_volume_usdt", filters["min_24h_volume_usdt"]))
        for t in tickers:
            if not t.symbol.endswith("USDT"):
                continue
            if t.price_change_pct < min_squeeze_pct:
                continue
            if t.quote_volume < min_volume:
                continue
            if any(c.symbol == t.symbol for c in top):
                continue  # already a long candidate
            try:
                pts = fetch_oi_history(t.symbol, period="5m", limit=49)
                oi_4h = oi_change_pct(pts)
                oi_1h = oi_change_pct(pts[-13:] if len(pts) >= 13 else pts)
            except Exception:  # noqa: BLE001
                continue
            if oi_4h >= 0:
                continue  # not a squeeze — real momentum, skip short
            liquidity_score = _bounded_score(_zscore([math.log10(max(t.quote_volume, 1.0))])[0])
            score = min(t.price_change_pct / 10.0, 5.0) + abs(oi_4h) / 10.0 + liquidity_score
            short_candidates.append(Candidate(
                symbol=t.symbol,
                last_price=t.last_price,
                price_change_pct=t.price_change_pct,
                quote_volume_usdt=t.quote_volume,
                oi_change_1h_pct=oi_1h,
                oi_change_4h_pct=oi_4h,
                trend_score=0.0,
                oi_score=0.0,
                liquidity_score=liquidity_score,
                score=score,
                side="SHORT",
            ))
        short_candidates.sort(key=lambda c: c.score, reverse=True)
        short_candidates = short_candidates[:short_cfg.get("top_n", 5)]
        logger.info("short candidates: {}", len(short_candidates))

    return {
        "generated_at_ms": int(time.time() * 1000),
        "regime": regime,
        "count": len(top),
        "candidates": [asdict(c) for c in top],
        "short_candidates": [asdict(c) for c in short_candidates],
        "filter_reason_stats": dict(filter_reason_stats),
    }
