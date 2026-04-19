"""Centralized exit evaluation: stop-loss, take-profit, trailing, timeout.

All position exit logic lives here. monitor.py calls evaluate_all() only.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger

from lana_bot.risk.exit_rules import ExitRuleConfig, ExitState, evaluate_exit_rules
from lana_bot.risk.risk_engine import record_profit_close
from lana_bot.risk.stop_loss import should_stop_out, unrealized_pnl_usdt
from lana_bot.state import journal, positions

EXIT_STATE_FILE = Path(__file__).resolve().parents[3] / "data" / "exit_state.json"


def evaluate_all(client, max_loss: float, exit_cfg: ExitRuleConfig) -> None:
    """Evaluate all exit rules for every open position. Called by monitor every poll."""
    state = _load_exit_state()
    changed = False
    open_symbols = {pos.symbol for pos in positions.list_positions()}

    for symbol in set(state) - open_symbols:
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

        effective_max_loss = pos.max_stop_loss_usdt if pos.max_stop_loss_usdt is not None else max_loss
        if should_stop_out(pos, mark, effective_max_loss):
            _trigger_stop_loss(client, pos, mark, pnl, effective_max_loss)
            state.pop(pos.symbol, None)
            changed = True
            continue

        decision = evaluate_exit_rules(
            pos=pos,
            pnl_usdt=pnl,
            max_loss_usdt=effective_max_loss,
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
            client.close(pos.symbol, exit_trigger=decision.exit_type, fraction=decision.close_fraction)
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
            if decision.close_fraction >= 1.0:
                state.pop(pos.symbol, None)
            if pnl > 0:
                record_profit_close(pos.symbol, position_id=pos.position_id)
            changed = True
        except Exception as e:  # noqa: BLE001
            logger.error("close failed during {} for {}: {}", decision.exit_type, pos.symbol, e)
            journal.log(
                "error",
                {"op": f"{decision.exit_type}_close", "symbol": pos.symbol, "error": str(e)},
            )

    if changed:
        _save_exit_state(state)


def _trigger_stop_loss(client, pos, mark: float, pnl: float, max_loss: float) -> None:
    logger.warning(
        "STOP-LOSS triggered for {} — pnl={:.2f}U entry={} mark={}",
        pos.symbol, pnl, pos.entry_price, mark,
    )
    try:
        client.close(pos.symbol, exit_trigger="hard_sl")
        journal.log(
            "stop_loss_triggered",
            {
                "symbol": pos.symbol,
                "exit_type": "hard_sl",
                "mark": mark,
                "pnl_usdt": pnl,
                "max_loss_usdt": max_loss,
                "loss_basis": "unrealized_pnl_on_notional_usdt",
            },
        )
        # Direct call — no log parsing required for cooldown to work
        from lana_bot.risk.risk_engine import record_stop_loss
        record_stop_loss(pos.symbol)
    except Exception as e:  # noqa: BLE001
        logger.error("close failed during stop-loss for {}: {}", pos.symbol, e)
        journal.log(
            "error",
            {"op": "stop_loss_close", "symbol": pos.symbol, "error": str(e)},
        )


def _load_exit_state() -> dict[str, ExitState]:
    if not EXIT_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(EXIT_STATE_FILE.read_text())
        return {sym: ExitState(**st) for sym, st in data.items()}
    except Exception:  # noqa: BLE001
        return {}


def _save_exit_state(state: dict[str, ExitState]) -> None:
    EXIT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = EXIT_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({sym: vars(st) for sym, st in state.items()}, indent=2))
    tmp.replace(EXIT_STATE_FILE)
