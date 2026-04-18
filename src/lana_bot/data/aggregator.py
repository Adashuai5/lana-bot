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
    fetch_oi_history,
    oi_change_pct,
)


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
    square_mentions: int = 0  # populated later when square scraper lands


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

    filter_reason_stats: Counter[str] = Counter()

    # Stage 1: legacy hard filters.
    stage1_passed = []
    for t in tickers:
        stage1_failed = False
        if t.quote_volume < filters["min_24h_volume_usdt"]:
            filter_reason_stats["stage1_low_volume"] += 1
            stage1_failed = True
        if t.price_change_pct < filters["min_24h_change_pct"]:
            filter_reason_stats["stage1_low_24h_change"] += 1
            stage1_failed = True
        if not stage1_failed:
            stage1_passed.append(t)

    # Sort by raw gain first so we cap OI-history fetches to top movers
    stage1_passed.sort(key=lambda t: t.price_change_pct, reverse=True)
    prefiltered = stage1_passed[: filters["top_n_candidates"] * 2]
    logger.info("stage1 filtered to {}", len(prefiltered))

    min_oi_4h = float(filters.get("min_oi_change_4h_pct", 0))
    min_cont_1h = float(filters.get("min_oi_continuity_1h", 0.55))
    min_cont_4h = float(filters.get("min_oi_continuity_4h", 0.50))
    max_step_vol = float(filters.get("max_oi_step_volatility_pct", 1.5))
    require_slope_consistency = bool(filters.get("require_oi_slope_consistency", True))

    # Stage 2: OI stability constraints.
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
        continuity_1h = _continuity_ratio(oi_1h_values)
        continuity_4h = _continuity_ratio(oi_values)
        step_volatility = _step_volatility_pct(oi_values)
        slope_1h = _slope(oi_1h_values)
        slope_4h = _slope(oi_values)

        stage2_failed = False
        if oi_pct_1h < filters["min_oi_change_1h_pct"]:
            filter_reason_stats["stage2_low_oi_change_1h"] += 1
            stage2_failed = True
        if oi_pct_4h < min_oi_4h:
            filter_reason_stats["stage2_low_oi_change_4h"] += 1
            stage2_failed = True
        if continuity_1h < min_cont_1h:
            filter_reason_stats["stage2_low_continuity_1h"] += 1
            stage2_failed = True
        if continuity_4h < min_cont_4h:
            filter_reason_stats["stage2_low_continuity_4h"] += 1
            stage2_failed = True
        if step_volatility > max_step_vol:
            filter_reason_stats["stage2_high_oi_volatility"] += 1
            stage2_failed = True
        if require_slope_consistency and slope_1h * slope_4h < 0:
            filter_reason_stats["stage2_slope_inconsistent"] += 1
            stage2_failed = True
        if stage2_failed:
            continue

        stage2_rows.append(
            {
                "symbol": t.symbol,
                "last_price": t.last_price,
                "price_change_pct": t.price_change_pct,
                "quote_volume_usdt": t.quote_volume,
                "oi_change_1h_pct": oi_pct_1h,
                "oi_change_4h_pct": oi_pct_4h,
                "square_mentions": square_mentions.get(t.symbol, 0),
            }
        )

    logger.info("stage2 filtered to {}", len(stage2_rows))

    candidates: list[Candidate] = []
    if stage2_rows:
        trend_raw = [float(r["price_change_pct"]) for r in stage2_rows]
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
                )
            )

    for c in candidates:
        if c.score <= 0:
            filter_reason_stats["stage2_non_positive_score"] += 1


    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[: filters["top_n_candidates"]]

    return {
        "generated_at_ms": int(time.time() * 1000),
        "count": len(top),
        "candidates": [asdict(c) for c in top],
        "filter_reason_stats": dict(filter_reason_stats),
    }
