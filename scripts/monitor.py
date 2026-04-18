"""Stop-loss daemon — polls each open position every N seconds and closes it
when unrealized loss exceeds the configured cap.

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
from lana_bot.risk.stop_loss import should_stop_out, unrealized_pnl_usdt  # noqa: E402
from lana_bot.state import journal, positions  # noqa: E402

POLL_SECONDS = 10


def check_once(client, max_loss: float) -> None:
    for pos in positions.list_positions():
        try:
            mark = client.get_mark_price(pos.symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("mark price fetch failed for {}: {}", pos.symbol, e)
            continue

        pnl = unrealized_pnl_usdt(pos, mark)
        if should_stop_out(pos, mark, max_loss):
            logger.warning(
                "STOP-LOSS triggered for {} — pnl={:.2f}U entry={} mark={}",
                pos.symbol, pnl, pos.entry_price, mark,
            )
            try:
                client.close(pos.symbol)
                journal.log(
                    "stop_loss_triggered",
                    {"symbol": pos.symbol, "mark": mark, "pnl_usdt": pnl},
                )
            except Exception as e:  # noqa: BLE001
                logger.error("close failed during stop-loss for {}: {}", pos.symbol, e)
                journal.log(
                    "error",
                    {"op": "stop_loss_close", "symbol": pos.symbol, "error": str(e)},
                )


def main() -> int:
    logger.add(LOGS_DIR / "monitor.log", rotation="10 MB", retention="14 days")
    logger.info("stop-loss monitor starting, poll={}s", POLL_SECONDS)

    while True:
        try:
            # live_trading flag may flip mid-run — refetch the client each loop
            # so dry→live switch requires only a config reload here too.
            from lana_bot.config import reload as _reload
            _reload()
            cfg = strategy()
            client = get_client()
            max_loss = float(cfg["max_stop_loss_per_position_usdt"])
            logger.info(
                "monitor config: max_stop_loss_per_position_usdt={}",
                max_loss,
            )
            check_once(client, max_loss)
        except Exception as e:  # noqa: BLE001
            logger.error("monitor loop error: {}", e)
            journal.log("error", {"op": "monitor_loop", "error": str(e)})
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
