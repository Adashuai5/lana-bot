"""Risk monitor daemon for hard stop-loss + structured profit-protection exits.

Runs forever; managed by launchd with KeepAlive.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lana_bot.config import LOGS_DIR, strategy  # noqa: E402
from lana_bot.execution import get_client  # noqa: E402
from lana_bot.risk.exit_rules import ExitRuleConfig, ExitState, evaluate_exit_rules  # noqa: E402
from lana_bot.risk.stop_loss import should_stop_out, unrealized_pnl_usdt  # noqa: E402
from lana_bot.state import journal, positions  # noqa: E402

POLL_SECONDS = 10
EXIT_STATE_FILE = Path(__file__).resolve().parents[1] / "data" / "exit_state.json"


def _load_exit_state() -> dict[str, ExitState]:
    if not EXIT_STATE_FILE.exists():
        return {}
    data = json.loads(EXIT_STATE_FILE.read_text())
    return {sym: ExitState(**st) for sym, st in data.items()}


def _save_exit_state(state: dict[str, ExitState]) -> None:
    EXIT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EXIT_STATE_FILE.write_text(
        json.dumps({sym: vars(st) for sym, st in state.items()}, indent=2),
    )


def check_once(client, max_loss: float, exit_cfg: ExitRuleConfig) -> None:
    state = _load_exit_state()
    changed = False
    open_symbols = {pos.symbol for pos in positions.list_positions()}

    stale_symbols = set(state) - open_symbols
    for symbol in stale_symbols:
        state.pop(symbol, None)
        changed = True

    for pos in positions.list_positions():
        try:
            mark = client.get_mark_price(pos.symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("mark price fetch failed for {}: {}", pos.symbol, e)
            continue

        pnl = unrealized_pnl_usdt(pos, mark)
        hold_seconds = max(0, int(time.time() - pos.entry_ts_ms / 1000))
        pos_state = state.setdefault(pos.symbol, ExitState())
        old_peak = pos_state.peak_pnl_usdt
        pos_state.peak_pnl_usdt = max(pos_state.peak_pnl_usdt, pnl)
        if pos_state.peak_pnl_usdt != old_peak:
            changed = True

        if should_stop_out(pos, mark, max_loss):
            logger.warning(
                "STOP-LOSS triggered for {} — pnl={:.2f}U entry={} mark={}",
                pos.symbol, pnl, pos.entry_price, mark,
            )
            try:
                client.close(pos.symbol, exit_trigger="hard_sl")
                journal.log(
                    "exit_triggered",
                    {"symbol": pos.symbol, "exit_type": "hard_sl", "mark": mark, "pnl_usdt": pnl},
                )
                state.pop(pos.symbol, None)
                changed = True
            except Exception as e:  # noqa: BLE001
                logger.error("close failed during stop-loss for {}: {}", pos.symbol, e)
                journal.log(
                    "error",
                    {"op": "stop_loss_close", "symbol": pos.symbol, "error": str(e)},
                )
            continue

        decision = evaluate_exit_rules(
            pos=pos,
            pnl_usdt=pnl,
            max_loss_usdt=max_loss,
            hold_seconds=hold_seconds,
            state=pos_state,
            cfg=exit_cfg,
        )
        if decision is None:
            continue

        if decision.exit_type == "trailing_tp" and not pos_state.fixed_tp_hit and pnl > 0:
            pos_state.fixed_tp_hit = True
            changed = True

        logger.info(
            "EXIT triggered {} for {} — pnl={:.2f}U hold={}s reason={}",
            decision.exit_type, pos.symbol, pnl, hold_seconds, decision.reason,
        )
        try:
            client.close(
                pos.symbol,
                exit_trigger=decision.exit_type,
            )
            journal.log(
                "exit_triggered",
                {
                    "symbol": pos.symbol,
                    "exit_type": decision.exit_type,
                    "mark": mark,
                    "pnl_usdt": pnl,
                    "reason": decision.reason,
                    "close_fraction": decision.close_fraction,
                },
            )
            state.pop(pos.symbol, None)
            changed = True
        except Exception as e:  # noqa: BLE001
            logger.error("close failed during {} for {}: {}", decision.exit_type, pos.symbol, e)
            journal.log(
                "error",
                {"op": f"{decision.exit_type}_close", "symbol": pos.symbol, "error": str(e)},
            )

    if changed:
        _save_exit_state(state)


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
