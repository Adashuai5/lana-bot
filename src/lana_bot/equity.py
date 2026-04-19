"""Equity calculator — derives current account equity and all dynamic risk thresholds.

equity = initial_capital_usdt + sum(net_pnl_usdt from all 'close' events in journal)

Uses realized-only equity (excludes unrealized PnL) to avoid a feedback loop where
open floating gains immediately inflate position sizes on the next cycle.
"""
from __future__ import annotations

import json

from lana_bot.config import DATA_DIR

JOURNAL_FILE = DATA_DIR / "journal.ndjson"


def realized_equity(initial_capital: float) -> float:
    """Return equity based on realized PnL only."""
    realized = 0.0
    if not JOURNAL_FILE.exists():
        return initial_capital
    with JOURNAL_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "close":
                realized += float(rec.get("net_pnl_usdt", 0.0))
    return initial_capital + realized


def derive_sizing(cfg: dict) -> dict:
    """Compute all dynamic USDT thresholds from current equity + config percentages.

    All ratios default to the same proportions as the original hardcoded values,
    so at 50U equity the output is identical to the old fixed config.

    Returns dict with keys:
      equity_usdt, position_size_usdt, max_stop_loss_per_position_usdt,
      atr_stop_min_usdt, atr_stop_max_usdt,
      max_daily_loss_usdt, max_unrealized_drawdown_usdt, trailing_drawdown_usdt
    """
    initial = float(cfg.get("initial_capital_usdt", 50))
    equity = realized_equity(initial)

    # Position sizing
    pos_pct = float(cfg.get("position_size_pct", 0.40))
    pos_min = float(cfg.get("position_size_min_usdt", 5.0))
    pos_max = float(cfg.get("position_size_max_usdt", 200.0))
    position_size = max(pos_min, min(pos_max, equity * pos_pct))

    # Per-position stop loss (% of position margin)
    sl_pct = float(cfg.get("max_stop_loss_pct_of_position", 0.50))
    max_sl = position_size * sl_pct

    # ATR stop range (% of position size)
    risk = cfg.get("risk", {})
    atr_min_pct = float(risk.get("atr_stop_min_pct_of_position", 0.40))
    atr_max_pct = float(risk.get("atr_stop_max_pct_of_position", 0.70))

    # Account-level circuit breakers (% of equity)
    daily_loss_pct = float(risk.get("max_daily_loss_pct", 0.60))
    unrealized_dd_pct = float(risk.get("max_unrealized_drawdown_pct", 0.40))

    # Trailing drawdown after TP (% of position size)
    exit_rules = cfg.get("exit_rules", {})
    trailing_pct = float(exit_rules.get("trailing_drawdown_pct", 0.15))

    return {
        "equity_usdt": round(equity, 4),
        "position_size_usdt": round(position_size, 4),
        "max_stop_loss_per_position_usdt": round(max_sl, 4),
        "atr_stop_min_usdt": round(position_size * atr_min_pct, 4),
        "atr_stop_max_usdt": round(position_size * atr_max_pct, 4),
        "max_daily_loss_usdt": round(equity * daily_loss_pct, 4),
        "max_unrealized_drawdown_usdt": round(equity * unrealized_dd_pct, 4),
        "trailing_drawdown_usdt": round(position_size * trailing_pct, 4),
    }
