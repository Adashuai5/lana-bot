"""Combine ticker gains + OI change into a ranked candidate list.

Outputs a dict ready to be JSON-dumped for Claude to consume.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from loguru import logger

from lana_bot.config import strategy
from lana_bot.data.binance_futures import (
    fetch_all_24h_tickers,
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
    score: float
    square_mentions: int = 0  # populated later when square scraper lands


def _score(
    price_change_pct: float,
    oi_change_1h_pct: float,
    quote_volume_usdt: float,
    square_mentions: int,
) -> float:
    # Simple additive score; tweak weights as the strategy matures.
    # Volume is log-scaled so a 100M-vol coin doesn't dwarf everything.
    import math

    vol_component = math.log10(max(quote_volume_usdt, 1)) * 2
    return (
        price_change_pct
        + oi_change_1h_pct * 1.5
        + vol_component
        + square_mentions * 3
    )


def build_candidates(square_mentions: dict[str, int] | None = None) -> dict:
    cfg = strategy()
    filters = cfg["filters"]
    square_mentions = square_mentions or {}

    tickers = fetch_all_24h_tickers()
    logger.info("fetched {} USDT-perp tickers", len(tickers))
    regime = safe_compute_market_regime(tickers_24h=tickers, cfg=cfg)

    # First pass: rule-based filter
    prefiltered = [
        t for t in tickers
        if t.quote_volume >= filters["min_24h_volume_usdt"]
        and t.price_change_pct >= filters["min_24h_change_pct"]
    ]
    # Sort by raw gain first so we cap OI-history fetches to top movers
    prefiltered.sort(key=lambda t: t.price_change_pct, reverse=True)
    prefiltered = prefiltered[: filters["top_n_candidates"] * 2]
    logger.info("prefiltered to {}", len(prefiltered))

    # OI change for each (these are N extra HTTP calls — kept bounded above)
    candidates: list[Candidate] = []
    for t in prefiltered:
        try:
            oi_points = fetch_oi_history(t.symbol)
            oi_pct = oi_change_pct(oi_points)
        except Exception as e:  # noqa: BLE001
            logger.warning("oi fetch failed for {}: {}", t.symbol, e)
            oi_pct = 0.0

        mentions = square_mentions.get(t.symbol, 0)
        score = _score(t.price_change_pct, oi_pct, t.quote_volume, mentions)
        candidates.append(
            Candidate(
                symbol=t.symbol,
                last_price=t.last_price,
                price_change_pct=t.price_change_pct,
                quote_volume_usdt=t.quote_volume,
                oi_change_1h_pct=oi_pct,
                score=score,
                square_mentions=mentions,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[: filters["top_n_candidates"]]

    return {
        "generated_at_ms": int(time.time() * 1000),
        "regime": regime,
        "count": len(top),
        "candidates": [asdict(c) for c in top],
    }
