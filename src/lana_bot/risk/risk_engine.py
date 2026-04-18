"""Unified risk control entry point. Direct state, no log parsing.

record_stop_loss() must be called by exit_engine on every hard stop-loss.
can_open() is the single gate for all pre-open checks.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from lana_bot.config import DATA_DIR
from lana_bot.risk.circuit_breaker import BreakerDecision, check_can_open

RISK_STATE_FILE = DATA_DIR / "risk_state.json"


def record_stop_loss(symbol: str, ts_ms: int | None = None) -> None:
    """Record a stop-loss event directly to risk_state.json (not via log parsing)."""
    ts_ms = ts_ms or int(time.time() * 1000)
    state = _load()
    state["last_stop_loss_ts_ms"] = max(state.get("last_stop_loss_ts_ms", 0), ts_ms)
    state.setdefault("stop_loss_log", []).append({"symbol": symbol, "ts_ms": ts_ms})
    _save(state)


def last_stop_loss_ts_ms() -> int | None:
    v = _load().get("last_stop_loss_ts_ms", 0)
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


def _load() -> dict:
    try:
        if RISK_STATE_FILE.exists():
            return json.loads(RISK_STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save(state: dict) -> None:
    RISK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RISK_STATE_FILE.write_text(json.dumps(state, indent=2))
