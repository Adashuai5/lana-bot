"""Per-symbol adaptive score persisted in data/state.json (scores section).

Score starts at 1.0, decays on SL, recovers on profitable close.
Pre-open gate: score < 0.5 → deny.
"""
from __future__ import annotations

import time

from lana_bot.state import risk_score_state

_INITIAL: float = 1.0
_SL_FACTOR: float = 0.7
_PROFIT_FACTOR: float = 1.1
_PROFIT_CAP: float = 1.5
_DENY_THRESHOLD: float = 0.5
_CLOSE_TTL: float = 86400.0  # 24h dedup window for processed_closes


def get_score(symbol: str) -> float:
    return risk_score_state.load()["scores"].get(symbol, _INITIAL)


def update_sl(symbol: str) -> float:
    """Decay score after a stop-loss. Prefer record_stop_loss in risk_engine for combined write."""
    state = risk_score_state.load()
    state["scores"][symbol] = round(state["scores"].get(symbol, _INITIAL) * _SL_FACTOR, 4)
    risk_score_state.save(state)
    return state["scores"][symbol]


def update_profit(symbol: str) -> float:
    """Boost score after a profitable close (no dedup)."""
    state = risk_score_state.load()
    state["scores"][symbol] = round(
        min(state["scores"].get(symbol, _INITIAL) * _PROFIT_FACTOR, _PROFIT_CAP), 4
    )
    risk_score_state.save(state)
    return state["scores"][symbol]


def record_profit_close(symbol: str, position_id: str | None = None) -> None:
    """Boost score after a profitable close, with position_id dedup (24h TTL).

    If position_id is provided, the boost is applied at most once per position.
    This prevents double-counting when both execute.py and exit_engine fire for
    the same position.
    """
    state = risk_score_state.load()

    if position_id:
        processed = state["risk"].setdefault("processed_closes", {})
        # Evict entries older than TTL
        cutoff = time.time() - _CLOSE_TTL
        state["risk"]["processed_closes"] = {k: v for k, v in processed.items() if v > cutoff}
        processed = state["risk"]["processed_closes"]

        if position_id in processed:
            risk_score_state.save(state)  # persist TTL eviction
            return
        processed[position_id] = time.time()

    state["scores"][symbol] = round(
        min(state["scores"].get(symbol, _INITIAL) * _PROFIT_FACTOR, _PROFIT_CAP), 4
    )
    risk_score_state.save(state)


def score_allows_open(symbol: str) -> tuple[bool, float]:
    score = get_score(symbol)
    return score >= _DENY_THRESHOLD, score
