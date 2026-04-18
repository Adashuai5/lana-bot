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
from lana_bot.execution import get_client  # noqa: E402
from lana_bot.risk.circuit_breaker import check_can_open  # noqa: E402
from lana_bot.state import journal, positions  # noqa: E402


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
        if current >= cap:
            logger.warning("position cap reached ({}), skipping remaining opens", cap)
            journal.log("skip", {"reason": "cap_reached", "symbol": item["symbol"]})
            break

        breaker = check_can_open(cfg)
        if not breaker.allowed:
            logger.warning("circuit breaker tripped: {}", breaker.reason)
            journal.log("skip", {"reason": breaker.reason, "symbol": item["symbol"]})
            break

        symbol = item["symbol"]
        size = item["size_usdt"] if item.get("size_usdt") else cfg["position_size_usdt"]
        leverage = item["leverage"] if item.get("leverage") else cfg["leverage"]
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
