"""Unified risk control entry point. Direct state, no log parsing.

record_stop_loss() must be called by exit_engine on every hard stop-loss.
can_open() is the single gate for all pre-open checks.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from lana_bot.risk.circuit_breaker import BreakerDecision, check_can_open
from lana_bot.state import risk_score_state

_SL_FACTOR: float = 0.7


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record_stop_loss(symbol: str, ts_ms: int | None = None) -> None:
    """Record a stop-loss event. Updates risk state and score in one atomic write."""
    ts_ms = ts_ms or int(time.time() * 1000)
    state = risk_score_state.load()
    risk = state["risk"]

    risk["last_stop_loss_ts_ms"] = max(risk.get("last_stop_loss_ts_ms", 0), ts_ms)
    risk.setdefault("stop_loss_log", []).append({"symbol": symbol, "ts_ms": ts_ms})

    today = _today_utc()
    sym = risk.setdefault("symbol_history", {}).setdefault(symbol, {})
    if sym.get("daily_sl_date") != today:
        sym["daily_sl_count"] = 0
        sym["daily_sl_date"] = today
    sym["daily_sl_count"] = sym.get("daily_sl_count", 0) + 1
    sym["last_sl_ts_ms"] = ts_ms

    # Update score inline (same atomic write, avoids double-save race)
    state["scores"][symbol] = round(state["scores"].get(symbol, 1.0) * _SL_FACTOR, 4)

    risk_score_state.save(state)


def record_profit_close(symbol: str, position_id: str | None = None) -> None:
    """Call after a profitable close to recover symbol score."""
    from lana_bot.risk.symbol_score import record_profit_close as _rpc
    _rpc(symbol, position_id=position_id)


def get_symbol_history(symbol: str) -> dict:
    return risk_score_state.load()["risk"].get("symbol_history", {}).get(symbol, {})


def last_stop_loss_ts_ms() -> int | None:
    v = risk_score_state.load()["risk"].get("last_stop_loss_ts_ms", 0)
    return v or None


def can_open(
    cfg: dict,
    *,
    pending_symbol: str | None = None,
    pending_size_usdt: float = 0.0,
    pending_leverage: int = 1,
) -> BreakerDecision:
    """Unified entry point for all pre-open risk checks."""
    return check_can_open(
        cfg,
        pending_symbol=pending_symbol,
        pending_size_usdt=pending_size_usdt,
        pending_leverage=pending_leverage,
    )
