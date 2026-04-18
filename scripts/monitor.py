"""Risk monitor daemon — schedules exit_engine every poll interval.

Runs forever; managed by launchd with KeepAlive.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lana_bot.config import LOGS_DIR, strategy  # noqa: E402
from lana_bot.execution import get_client  # noqa: E402
from lana_bot.risk.exit_engine import evaluate_all  # noqa: E402
from lana_bot.risk.exit_rules import ExitRuleConfig  # noqa: E402
from lana_bot.state import journal  # noqa: E402

POLL_SECONDS = 10


def check_once(client, max_loss: float, exit_cfg: ExitRuleConfig) -> None:
    evaluate_all(client, max_loss, exit_cfg)


def main() -> int:
    logger.add(LOGS_DIR / "monitor.log", rotation="10 MB", retention="14 days")
    logger.info("stop-loss monitor starting, poll={}s", POLL_SECONDS)

    while True:
        try:
            from lana_bot.config import reload as _reload
            _reload()
            cfg = strategy()
            client = get_client()
            max_loss = float(cfg["max_stop_loss_per_position_usdt"])
            ecfg = cfg.get("exit_rules", {})
            exit_cfg = ExitRuleConfig(
                risk_multiple_tp=float(ecfg.get("risk_multiple_tp", 1.5)),
                risk_multiple_tp_close_fraction=float(ecfg.get("risk_multiple_tp_close_fraction", 0.5)),
                trailing_drawdown_usdt=float(ecfg.get("trailing_drawdown_usdt", 3)),
                max_hold_seconds=int(ecfg.get("max_hold_seconds", 6 * 60 * 60)),
            )
            check_once(client, max_loss, exit_cfg)
        except Exception as e:  # noqa: BLE001
            logger.error("monitor loop error: {}", e)
            journal.log("error", {"op": "monitor_loop", "error": str(e)})
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
