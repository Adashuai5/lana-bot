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
from lana_bot.risk.circuit_breaker import check_can_open  # noqa: E402
from lana_bot.state import journal, positions  # noqa: E402


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
            client.close(symbol)
        except Exception as e:  # noqa: BLE001
            logger.error("close failed for {}: {}", symbol, e)
            journal.log("error", {"op": "close", "symbol": symbol, "error": str(e)})

    # Respect max_concurrent_positions cap
    current = len(positions.list_positions())
    cap = cfg["max_concurrent_positions"]

    for item in decision.get("open", []):
        if gate and gate.state == "block":
            logger.warning("regime blocked opens: {}", gate.reasons)
            journal.log("skip", {"reason": "regime_block", "symbol": item["symbol"], "details": gate.reasons})
            break

        if current >= cap:
            logger.warning("position cap reached ({}), skipping remaining opens", cap)
            journal.log("skip", {"reason": "cap_reached", "symbol": item["symbol"]})
            break

        symbol = item["symbol"]
        size = item["size_usdt"] if item.get("size_usdt") else cfg["position_size_usdt"]
        if gate and gate.state == "reduce":
            size = round(float(size) * gate.size_multiplier, 8)
        leverage = item["leverage"] if item.get("leverage") else cfg["leverage"]

        breaker = check_can_open(
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

        try:
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
