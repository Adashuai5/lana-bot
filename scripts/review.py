"""Performance review — computes weekly stats from journal.ndjson and writes data/reviews/latest.json.

Called by scripts/review_cycle.sh before Claude weekly review.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lana_bot.config import DATA_DIR
from lana_bot.equity import JOURNAL_FILE, realized_equity

REVIEW_DIR = DATA_DIR / "reviews"
PERIOD_DAYS = 7


def _iter_events(since_ts_ms: float):
    if not JOURNAL_FILE.exists():
        return
    with JOURNAL_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(rec.get("ts_ms", 0)) >= since_ts_ms:
                yield rec


def compute_stats(period_days: int = PERIOD_DAYS) -> dict:
    since_ms = (time.time() - period_days * 86400) * 1000

    closes: list[dict] = []
    daily_loss_cap_triggers = 0
    symbol_wins: dict[str, float] = {}
    symbol_losses: dict[str, float] = {}
    hold_minutes: list[float] = []

    for rec in _iter_events(since_ms):
        event = rec.get("event")
        if event == "close":
            pnl = float(rec.get("net_pnl_usdt", 0.0))
            closes.append(rec)
            sym = rec.get("symbol", "")
            held_ms = float(rec.get("held_ms", 0))
            hold_minutes.append(held_ms / 60000)
            if pnl >= 0:
                symbol_wins[sym] = symbol_wins.get(sym, 0) + pnl
            else:
                symbol_losses[sym] = symbol_losses.get(sym, 0) + pnl
        elif event == "skip" and "daily_loss_cap" in str(rec.get("reason", "")):
            daily_loss_cap_triggers += 1

    total = len(closes)
    wins = [c for c in closes if float(c.get("net_pnl_usdt", 0)) >= 0]
    losses = [c for c in closes if float(c.get("net_pnl_usdt", 0)) < 0]

    win_rate = round(len(wins) / total * 100, 1) if total else 0.0
    avg_win = round(sum(float(c["net_pnl_usdt"]) for c in wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(float(c["net_pnl_usdt"]) for c in losses) / len(losses), 2) if losses else 0.0
    total_win_pnl = sum(float(c["net_pnl_usdt"]) for c in wins)
    total_loss_pnl = abs(sum(float(c["net_pnl_usdt"]) for c in losses))
    profit_factor = round(total_win_pnl / total_loss_pnl, 2) if total_loss_pnl > 0 else 0.0
    avg_hold = round(sum(hold_minutes) / len(hold_minutes), 1) if hold_minutes else 0.0

    # Running max drawdown (from journal start, not period)
    running_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    if JOURNAL_FILE.exists():
        with JOURNAL_FILE.open() as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if rec.get("event") == "close":
                    running_pnl += float(rec.get("net_pnl_usdt", 0))
                    peak = max(peak, running_pnl)
                    dd = running_pnl - peak
                    max_dd = min(max_dd, dd)

    import tomllib
    with open(Path(__file__).resolve().parents[1] / "config" / "strategy.toml", "rb") as f:
        cfg = tomllib.load(f)
    initial = float(cfg.get("initial_capital_usdt", 50))
    equity = realized_equity(initial)
    equity_vs_initial = round((equity - initial) / initial * 100, 1)

    top_wins = sorted(symbol_wins.items(), key=lambda x: -x[1])[:5]
    top_losses = sorted(symbol_losses.items(), key=lambda x: x[1])[:5]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": period_days,
        "equity_usdt": round(equity, 2),
        "equity_vs_initial_pct": equity_vs_initial,
        "total_trades": total,
        "win_rate_pct": win_rate,
        "avg_win_usdt": avg_win,
        "avg_loss_usdt": avg_loss,
        "profit_factor": profit_factor,
        "max_drawdown_usdt": round(max_dd, 2),
        "avg_hold_minutes": avg_hold,
        "daily_loss_cap_triggers": daily_loss_cap_triggers,
        "most_profitable_symbols": [s for s, _ in top_wins],
        "most_loss_symbols": [s for s, _ in top_losses],
    }


def main() -> int:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    stats = compute_stats()
    out = REVIEW_DIR / "latest.json"
    out.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"Review written to {out}")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
