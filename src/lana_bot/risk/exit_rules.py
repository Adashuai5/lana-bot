"""Structured exit rules for profit protection and stale-position cleanup."""
from __future__ import annotations

from dataclasses import dataclass

from lana_bot.state.positions import Position


@dataclass
class ExitDecision:
    exit_type: str
    reason: str
    close_fraction: float = 1.0


@dataclass
class ExitState:
    peak_pnl_usdt: float = 0.0
    fixed_tp_hit: bool = False


@dataclass
class ExitRuleConfig:
    risk_multiple_tp: float = 1.5
    risk_multiple_tp_close_fraction: float = 0.5
    trailing_drawdown_usdt: float = 3.0
    max_hold_seconds: int = 6 * 60 * 60


def evaluate_exit_rules(
    pos: Position,
    pnl_usdt: float,
    max_loss_usdt: float,
    hold_seconds: int,
    state: ExitState,
    cfg: ExitRuleConfig,
) -> ExitDecision | None:
    """Return an exit decision if any rule is triggered for the given position."""
    if hold_seconds >= cfg.max_hold_seconds:
        return ExitDecision(
            exit_type="time_stop",
            reason=f"held {hold_seconds}s >= {cfg.max_hold_seconds}s",
        )

    if not state.fixed_tp_hit and max_loss_usdt > 0:
        tp_pnl = cfg.risk_multiple_tp * max_loss_usdt
        if pnl_usdt >= tp_pnl:
            return ExitDecision(
                exit_type="trailing_tp",
                reason=f"hit +{cfg.risk_multiple_tp:.2f}R ({pnl_usdt:.2f}U >= {tp_pnl:.2f}U)",
                close_fraction=cfg.risk_multiple_tp_close_fraction,
            )

    if state.fixed_tp_hit and pnl_usdt > 0:
        retrace = state.peak_pnl_usdt - pnl_usdt
        if retrace >= cfg.trailing_drawdown_usdt:
            return ExitDecision(
                exit_type="trailing_tp",
                reason=f"drawdown {retrace:.2f}U >= {cfg.trailing_drawdown_usdt:.2f}U",
            )
    return None
