"""Read a decision JSON and apply opens/closes via the active exchange client.

Usage: python scripts/execute.py <decision_file>
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lana_bot.config import DATA_DIR, LOGS_DIR, strategy  # noqa: E402
from lana_bot.data.market_regime import decide_regime_gate  # noqa: E402
from lana_bot.execution import get_client  # noqa: E402
from lana_bot.risk.circuit_breaker import DAY_MS, _iter_recent_events, daily_realized_pnl_usdt  # noqa: E402
from lana_bot.risk.risk_engine import can_open as _risk_can_open, record_profit_close  # noqa: E402
from lana_bot.risk.stop_loss import unrealized_pnl_usdt  # noqa: E402
from lana_bot.state import journal, positions, risk_score_state  # noqa: E402

_ACTION_TTL: float = 86400.0  # 24h dedup window for executed_actions


def _make_action_id(decision_ts: str, symbol: str, action: str) -> str:
    raw = f"{decision_ts}:{symbol}:{action}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]  # noqa: S324


def _check_and_record_action(action_id: str) -> bool:
    """Return True if action was already executed (skip). False if new (record and proceed)."""
    state = risk_score_state.load()
    executed = state["risk"].setdefault("executed_actions", {})
    cutoff = time.time() - _ACTION_TTL
    state["risk"]["executed_actions"] = {k: v for k, v in executed.items() if v > cutoff}
    executed = state["risk"]["executed_actions"]
    if action_id in executed:
        risk_score_state.save(state)
        return True
    executed[action_id] = time.time()
    risk_score_state.save(state)
    return False


def _atr_stop(symbol: str, size_usdt: float, leverage: int, cfg: dict) -> float | None:
    """Compute ATR-based stop for this open. Returns None to fall back to config default."""
    risk = cfg.get("risk", {})
    multiplier = float(risk.get("atr_stop_multiplier", 0) or 0)
    if multiplier <= 0:
        return None
    min_stop = float(risk.get("atr_stop_min_usdt", 0) or 0)
    max_stop = float(risk.get("atr_stop_max_usdt", 0) or 0)
    try:
        latest = DATA_DIR / "candidates" / "latest.json"
        payload = json.loads(latest.read_text())
        all_candidates = payload.get("candidates", []) + payload.get("short_candidates", [])
        row = next((c for c in all_candidates if c["symbol"] == symbol), None)
        atr_pct = float(row["atr_pct"]) if row and row.get("atr_pct") else 0.0
    except Exception:  # noqa: BLE001
        return None
    if atr_pct <= 0:
        return None
    notional = size_usdt * leverage
    stop = atr_pct / 100 * notional * multiplier
    if min_stop > 0:
        stop = max(stop, min_stop)
    if max_stop > 0:
        stop = min(stop, max_stop)
    return round(stop, 4)


def _constraint_check(item: dict, cfg: dict, current: int) -> str | None:
    """Hard constraint filter on AI decisions. Returns rejection reason or None if allowed.

    Runs before circuit_breaker — AI suggestions are rejected here on hard rule violations.
    """
    cap = int(cfg["max_concurrent_positions"])
    if current >= cap:
        return f"cap_reached: {current}/{cap}"
    risk = cfg.get("risk", {})
    daily_loss_limit = float(risk.get("max_daily_loss_usdt", 0) or 0)
    if daily_loss_limit > 0:
        realized = daily_realized_pnl_usdt(_iter_recent_events(DAY_MS))
        if realized <= -daily_loss_limit:
            return f"daily_loss_cap: {realized:.2f}U"
    return None


def _resolve_regime_snapshot(decision: dict) -> dict:
    if isinstance(decision.get("regime"), dict):
        return decision["regime"]

    latest_candidates = DATA_DIR / "candidates" / "latest.json"
    if latest_candidates.exists():
        try:
            payload = json.loads(latest_candidates.read_text())
            if isinstance(payload.get("regime"), dict):
                return payload["regime"]
        except Exception:  # noqa: BLE001
            pass
    return {}


def _sync_positions(client, decision_ts: str) -> None:
    """Align local positions.json with exchange ground truth.

    Grace period: positions opened within the last 30s are excluded from
    ghost-position removal to avoid deleting a position that was just opened
    but not yet confirmed by the exchange.
    Sync failures are logged and skipped — they never block the decision loop.
    """
    if getattr(client, "is_stub", False):
        return  # simulator has no exchange to sync with

    try:
        exchange_map = {p["symbol"]: p for p in client.get_open_positions()}
    except Exception as e:  # noqa: BLE001
        journal.log("warn", {"msg": f"position sync failed, skipping: {e}"})
        return

    local_map = {p.symbol: p for p in positions.list_positions()}
    now = time.time()
    grace = 30.0

    # Local has / exchange doesn't → remove (ghost), with grace period
    for sym, pos in list(local_map.items()):
        if sym not in exchange_map:
            opened_age = now - pos.entry_ts_ms / 1000
            if opened_age < grace:
                journal.log("warn", {"msg": f"new position {sym} not yet on exchange (grace), skipping sync"})
                continue
            positions.remove(sym)
            journal.log("warn", {"msg": f"ghost position removed from local state: {sym}"})

    # Exchange has / local doesn't → recover (orphan)
    for sym, ex in exchange_map.items():
        if sym not in local_map:
            amt = float(ex.get("positionAmt", 0))
            side = "LONG" if amt > 0 else "SHORT"
            notional = abs(float(ex.get("notional", 0)))
            lev = int(float(ex.get("leverage", 1)))
            entry_price = float(ex.get("entryPrice", 0))
            pos = positions.Position(
                symbol=sym,
                side=side,
                entry_price=entry_price,
                size_usdt=round(notional / lev, 4) if lev else notional,
                leverage=lev,
                notional_usdt=notional,
                entry_ts_ms=int(now * 1000),
                mode="live",
            )
            positions.add(pos)
            journal.log("warn", {"msg": f"orphan position recovered into local state: {sym}"})

    # Size mismatch → correct local (no order sent)
    for sym in set(local_map) & set(exchange_map):
        local_notional = local_map[sym].notional_usdt
        ex_notional = abs(float(exchange_map[sym].get("notional", local_notional)))
        if abs(local_notional - ex_notional) > 1.0:
            positions.update_size(sym, ex_notional)
            journal.log("warn", {"msg": f"position size corrected for {sym}: {local_notional:.2f} → {ex_notional:.2f}"})


def main(decision_path: Path) -> int:
    logger.remove()
    logger.add(LOGS_DIR / "execute.log", rotation="10 MB", retention="14 days")
    cfg = strategy()
    client = get_client()
    logger.warning(
        "[安全告警] 即将执行交易流程 exchange={} live_trading={} client={}",
        cfg["exchange"],
        cfg["live_trading"],
        client.name,
    )
    logger.info("execute with client={} live={}", client.name, cfg["live_trading"])

    decision = json.loads(decision_path.read_text())
    decision_ts = decision_path.stem  # unix timestamp stem used as action_id seed
    journal.log("decision", {"source": str(decision_path), **decision})
    regime = _resolve_regime_snapshot(decision)
    regime_cfg = cfg.get("regime", {})
    gate_enabled = bool(regime_cfg.get("enabled", False) and regime_cfg.get("execute_gate_enabled", False))
    gate = decide_regime_gate(regime, cfg) if gate_enabled else None
    if gate:
        logger.info("regime gate={} size_multiplier={} reasons={}", gate.state, gate.size_multiplier, gate.reasons)
        journal.log(
            "regime_gate",
            {"state": gate.state, "size_multiplier": gate.size_multiplier, "reasons": gate.reasons},
        )

    # Sync local state with exchange before acting
    _sync_positions(client, decision_ts)

    # Closes first (free up slots)
    for item in decision.get("close", []):
        symbol = item["symbol"]
        action_id = _make_action_id(decision_ts, symbol, "close")
        if _check_and_record_action(action_id):
            journal.log("skip", {"reason": "duplicate_action", "symbol": symbol, "action_id": action_id})
            continue

        pos = positions.find(symbol)
        pre_close_pnl: float | None = None
        position_id: str | None = pos.position_id if pos else None
        if pos is not None:
            try:
                mark = client.get_mark_price(symbol)
                pre_close_pnl = unrealized_pnl_usdt(pos, mark)
            except Exception:  # noqa: BLE001
                pass
        try:
            client.close(symbol, exit_trigger=item.get("exit_trigger", "signal_decay"))
            if pre_close_pnl is not None and pre_close_pnl > 0:
                record_profit_close(symbol, position_id=position_id)
        except Exception as e:  # noqa: BLE001
            logger.error("close failed for {}: {}", symbol, e)
            journal.log("error", {"op": "close", "symbol": symbol, "error": str(e)})

    # Fail-closed: if positions.json was corrupt, block all opens
    if positions.is_corrupted():
        journal.log("warn", {"msg": "positions.json was corrupt at startup — all opens blocked (fail-closed)"})
        return 0

    current = len(positions.list_positions())

    for item in decision.get("open", []):
        if gate and gate.state == "block":
            logger.warning("regime blocked opens: {}", gate.reasons)
            journal.log("skip", {"reason": "regime_block", "symbol": item["symbol"], "details": gate.reasons})
            break

        symbol = item["symbol"]
        action_id = _make_action_id(decision_ts, symbol, "open")
        if _check_and_record_action(action_id):
            journal.log("skip", {"reason": "duplicate_action", "symbol": symbol, "action_id": action_id})
            continue

        # Hard constraint filter — AI suggestion rejected before circuit_breaker
        rejection = _constraint_check(item, cfg, current)
        if rejection:
            logger.warning("constraint filter rejected {}: {}", symbol, rejection)
            journal.log("skip", {"reason": rejection, "symbol": symbol, "layer": "constraint_filter"})
            break

        size = item["size_usdt"] if item.get("size_usdt") else cfg["position_size_usdt"]
        if gate and gate.state == "reduce":
            size = round(float(size) * gate.size_multiplier, 8)
        leverage = item["leverage"] if item.get("leverage") else cfg["leverage"]

        breaker = _risk_can_open(
            cfg,
            pending_symbol=symbol,
            pending_size_usdt=size,
            pending_leverage=leverage,
        )
        if not breaker.allowed:
            logger.warning("circuit breaker tripped: {}", breaker.reason)
            journal.log(
                "skip",
                {
                    "reason": breaker.reason,
                    "symbol": symbol,
                    "breaker_dimension": breaker.dimension,
                    "breaker_details": breaker.details,
                },
            )
            break

        side = item.get("side", "LONG").upper()
        max_stop_loss_usdt = _atr_stop(symbol, size, leverage, cfg)
        try:
            if side == "SHORT":
                client.open_short(symbol, size, leverage, max_stop_loss_usdt=max_stop_loss_usdt)
            else:
                client.open_long(symbol, size, leverage, max_stop_loss_usdt=max_stop_loss_usdt)
            current += 1
        except Exception as e:  # noqa: BLE001
            logger.error("open failed for {}: {}", symbol, e)
            journal.log("error", {"op": "open", "symbol": symbol, "error": str(e)})

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: execute.py <decision_file>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1])))
