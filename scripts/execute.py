"""Read a decision JSON and apply opens/closes via the active exchange client.

Usage: python scripts/execute.py <decision_file>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lana_bot.config import DATA_DIR, LOGS_DIR, strategy  # noqa: E402
from lana_bot.data.market_regime import decide_regime_gate  # noqa: E402
from lana_bot.execution import get_client  # noqa: E402
from lana_bot.risk.circuit_breaker import DAY_MS, _iter_recent_events, daily_realized_pnl_usdt  # noqa: E402
from lana_bot.risk.risk_engine import can_open as _risk_can_open  # noqa: E402
from lana_bot.state import journal, positions  # noqa: E402


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


def main(decision_path: Path) -> int:
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

    # Closes first (free up slots)
    for item in decision.get("close", []):
        symbol = item["symbol"]
        try:
            client.close(symbol, exit_trigger=item.get("exit_trigger", "signal_decay"))
        except Exception as e:  # noqa: BLE001
            logger.error("close failed for {}: {}", symbol, e)
            journal.log("error", {"op": "close", "symbol": symbol, "error": str(e)})

    current = len(positions.list_positions())

    for item in decision.get("open", []):
        if gate and gate.state == "block":
            logger.warning("regime blocked opens: {}", gate.reasons)
            journal.log("skip", {"reason": "regime_block", "symbol": item["symbol"], "details": gate.reasons})
            break

        symbol = item["symbol"]

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
        try:
            if side == "SHORT":
                client.open_short(symbol, size, leverage)
            else:
                client.open_long(symbol, size, leverage)
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
