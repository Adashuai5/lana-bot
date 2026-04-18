"""Account-level circuit breakers. Read-only over journal.ndjson.

Gates return a structured decision. execute.py must call check_can_open() before
_every_ open; if blocked, skip and journal the breaker dimension + details.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from lana_bot.config import DATA_DIR
from lana_bot.data.binance_futures import fetch_mark_price
from lana_bot.risk.stop_loss import unrealized_pnl_usdt
from lana_bot.state import positions

JOURNAL_FILE = DATA_DIR / "journal.ndjson"
DAY_MS = 24 * 60 * 60 * 1000

# Common meme clusters for correlation/crowding control.
# Fallback bucket is "other".
_MEME_TICKER_KEYWORDS = (
    "DOGE",
    "SHIB",
    "PEPE",
    "FLOKI",
    "BONK",
    "WIF",
    "BOME",
    "MEME",
    "BABYDOGE",
    "NEIRO",
    "MOG",
    "TURBO",
    "PNUT",
)


@dataclass
class BreakerDecision:
    allowed: bool
    reason: str
    dimension: str = "ok"
    details: dict = field(default_factory=dict)


def _deny(dimension: str, reason: str, **details) -> BreakerDecision:
    return BreakerDecision(False, reason=reason, dimension=dimension, details=details)


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


def _sector_for_symbol(symbol: str) -> str:
    s = symbol.upper()
    if any(key in s for key in _MEME_TICKER_KEYWORDS):
        return "meme"
    return "other"


def daily_realized_pnl_usdt(events: list[dict]) -> float:
    return sum(
        float(e.get("net_pnl_usdt", 0.0))
        for e in events
        if e.get("event") == "close"
    )


def combined_unrealized_pnl_usdt() -> tuple[float, list[str]]:
    total = 0.0
    failures: list[str] = []
    for pos in positions.list_positions():
        try:
            mark = fetch_mark_price(pos.symbol)
            total += unrealized_pnl_usdt(pos, mark)
        except Exception:  # noqa: BLE001
            failures.append(pos.symbol)
    return total, failures


def todays_opens(events: list[dict]) -> int:
    return sum(1 for e in events if e.get("event") == "open")


def recent_stop_loss_ts_ms(events: list[dict]) -> int | None:
    latest = 0
    for e in events:
        if e.get("event") == "stop_loss_triggered":
            latest = max(latest, int(e.get("ts_ms", 0)))
    return latest or None


def _sector_exposure_pct(
    *,
    sector: str,
    account_base_usdt: float,
    pending_symbol: str | None = None,
    pending_size_usdt: float = 0.0,
    pending_leverage: int = 1,
) -> tuple[float, float]:
    """Returns (sector_notional_usdt, sector_exposure_pct)."""
    sector_notional = sum(
        p.notional_usdt
        for p in positions.list_positions()
        if _sector_for_symbol(p.symbol) == sector
    )
    if pending_symbol and _sector_for_symbol(pending_symbol) == sector:
        sector_notional += pending_size_usdt * pending_leverage

    if account_base_usdt <= 0:
        return sector_notional, 0.0
    return sector_notional, sector_notional / account_base_usdt * 100.0


def check_can_open(
    cfg: dict,
    *,
    pending_symbol: str | None = None,
    pending_size_usdt: float = 0.0,
    pending_leverage: int = 1,
) -> BreakerDecision:
    """Evaluate all breakers. Returns the first failure, or allowed=True."""
    risk = cfg.get("risk", {})
    max_daily_loss = float(risk.get("max_daily_loss_usdt", 0) or 0)
    max_daily_opens = int(risk.get("max_daily_opens", 0) or 0)
    cooldown_min = float(risk.get("stop_loss_cooldown_min", 0) or 0)
    max_unrealized_dd = float(risk.get("max_unrealized_drawdown_usdt", 0) or 0)
    max_sector_exposure_pct = float(risk.get("max_sector_exposure_pct", 0) or 0)

    events_24h = _iter_recent_events(DAY_MS)

    if max_daily_loss > 0:
        realized = daily_realized_pnl_usdt(events_24h)
        if realized <= -max_daily_loss:
            return _deny(
                "daily_loss_cap",
                f"daily_loss_cap: realized={realized:.2f}U ≤ -{max_daily_loss}U",
                realized_pnl_usdt=round(realized, 4),
                max_daily_loss_usdt=max_daily_loss,
            )

    if max_unrealized_dd > 0:
        unrealized, failures = combined_unrealized_pnl_usdt()
        if failures:
            return _deny(
                "mark_price_unavailable",
                f"mark_price_unavailable: failed to price {','.join(failures)}",
                symbols=failures,
            )
        if unrealized <= -max_unrealized_dd:
            return _deny(
                "unrealized_drawdown_cap",
                f"unrealized_drawdown_cap: unrealized={unrealized:.2f}U ≤ -{max_unrealized_dd}U",
                unrealized_pnl_usdt=round(unrealized, 4),
                max_unrealized_drawdown_usdt=max_unrealized_dd,
            )

    if max_sector_exposure_pct > 0:
        account_base_usdt = float(cfg.get("initial_capital_usdt", 0) or 0)
        pending_sector = _sector_for_symbol(pending_symbol or "")
        sector_notional, sector_pct = _sector_exposure_pct(
            sector=pending_sector,
            account_base_usdt=account_base_usdt,
            pending_symbol=pending_symbol,
            pending_size_usdt=pending_size_usdt,
            pending_leverage=pending_leverage,
        )
        if sector_pct > max_sector_exposure_pct:
            return _deny(
                "sector_exposure_cap",
                (
                    f"sector_exposure_cap: sector={pending_sector} exposure={sector_pct:.1f}% "
                    f"> {max_sector_exposure_pct:.1f}%"
                ),
                sector=pending_sector,
                sector_notional_usdt=round(sector_notional, 4),
                sector_exposure_pct=round(sector_pct, 4),
                max_sector_exposure_pct=max_sector_exposure_pct,
            )

    if max_daily_opens > 0:
        opens = todays_opens(events_24h)
        if opens >= max_daily_opens:
            return _deny(
                "daily_open_cap",
                f"daily_open_cap: {opens}/{max_daily_opens} opens in 24h",
                opens_24h=opens,
                max_daily_opens=max_daily_opens,
            )

    if cooldown_min > 0:
        last_sl = recent_stop_loss_ts_ms(events_24h)
        if last_sl is not None:
            elapsed_min = (int(time.time() * 1000) - last_sl) / 60_000
            if elapsed_min < cooldown_min:
                return _deny(
                    "stop_loss_cooldown",
                    f"stop_loss_cooldown: {elapsed_min:.1f}min since last SL < {cooldown_min}min",
                    elapsed_min=round(elapsed_min, 4),
                    cooldown_min=cooldown_min,
                )

    return BreakerDecision(True, "ok", dimension="ok", details={})
