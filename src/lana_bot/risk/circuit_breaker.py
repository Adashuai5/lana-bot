"""Account-level circuit breakers. Read-only over journal.ndjson.

Three independent gates, each returns (allowed, reason). execute.py must call
`check_can_open()` before every open; if blocked, skip and journal the reason.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from lana_bot.config import DATA_DIR

JOURNAL_FILE = DATA_DIR / "journal.ndjson"
DAY_MS = 24 * 60 * 60 * 1000


@dataclass
class BreakerDecision:
    allowed: bool
    reason: str


def _iter_recent_events(window_ms: int) -> list[dict]:
    if not JOURNAL_FILE.exists():
        return []
    cutoff = int(time.time() * 1000) - window_ms
    out = []
    # Journal grows append-only; for a 30-min cycle bot the file stays small
    # enough that a full scan is fine. Revisit if it exceeds ~100MB.
    with JOURNAL_FILE.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts_ms", 0) >= cutoff:
                out.append(rec)
    return out


def daily_realized_pnl_usdt(events: list[dict]) -> float:
    return sum(
        float(e.get("net_pnl_usdt", 0.0))
        for e in events
        if e.get("event") == "close"
    )


def todays_opens(events: list[dict]) -> int:
    return sum(1 for e in events if e.get("event") == "open")


def recent_stop_loss_ts_ms(events: list[dict]) -> int | None:
    latest = 0
    for e in events:
        if e.get("event") == "stop_loss_triggered":
            latest = max(latest, int(e.get("ts_ms", 0)))
    return latest or None


def check_can_open(cfg: dict) -> BreakerDecision:
    """Evaluate all breakers. Returns the first failure, or allowed=True."""
    risk = cfg.get("risk", {})
    max_daily_loss = float(risk.get("max_daily_loss_usdt", 0) or 0)
    max_daily_opens = int(risk.get("max_daily_opens", 0) or 0)
    cooldown_min = float(risk.get("stop_loss_cooldown_min", 0) or 0)

    events_24h = _iter_recent_events(DAY_MS)

    if max_daily_loss > 0:
        realized = daily_realized_pnl_usdt(events_24h)
        if realized <= -max_daily_loss:
            return BreakerDecision(
                False,
                f"daily_loss_cap: realized={realized:.2f}U ≤ -{max_daily_loss}U",
            )

    if max_daily_opens > 0:
        opens = todays_opens(events_24h)
        if opens >= max_daily_opens:
            return BreakerDecision(
                False,
                f"daily_open_cap: {opens}/{max_daily_opens} opens in 24h",
            )

    if cooldown_min > 0:
        last_sl = recent_stop_loss_ts_ms(events_24h)
        if last_sl is not None:
            elapsed_min = (int(time.time() * 1000) - last_sl) / 60_000
            if elapsed_min < cooldown_min:
                return BreakerDecision(
                    False,
                    f"stop_loss_cooldown: {elapsed_min:.1f}min since last SL < {cooldown_min}min",
                )

    return BreakerDecision(True, "ok")
