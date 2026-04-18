"""Compute unrealized PnL for a position at a given mark price."""
from __future__ import annotations

from lana_bot.state.positions import Position


def unrealized_pnl_usdt(pos: Position, mark_price: float) -> float:
    """USDT PnL on the notional — simulator uses 0.04% * 2 taker fees on close."""
    price_move = (mark_price - pos.entry_price) / pos.entry_price
    if pos.side == "SHORT":
        price_move = -price_move
    return price_move * pos.notional_usdt


def should_stop_out(pos: Position, mark_price: float, max_loss_usdt: float) -> bool:
    return unrealized_pnl_usdt(pos, mark_price) <= -max_loss_usdt
