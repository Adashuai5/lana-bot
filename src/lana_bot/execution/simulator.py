"""Dry-run simulator — fills at current Binance mark price, no slippage, 0.04% fee.

Writes to the same positions.json + journal.ndjson as the live clients so the
rest of the pipeline is identical in dry-run and live modes.
"""
from __future__ import annotations

import time

from loguru import logger

from lana_bot.data.binance_futures import fetch_mark_price
from lana_bot.execution.base import FillResult
from lana_bot.state import journal, positions

TAKER_FEE = 0.0004  # 0.04% one side, applied to notional


class Simulator:
    name = "simulator"

    def get_mark_price(self, symbol: str) -> float:
        return fetch_mark_price(symbol)

    def open_long(self, symbol: str, size_usdt: float, leverage: int) -> FillResult:
        if positions.find(symbol) is not None:
            raise ValueError(f"already have position in {symbol}")
        price = fetch_mark_price(symbol)
        notional = size_usdt * leverage
        fee = notional * TAKER_FEE
        ts = int(time.time() * 1000)

        pos = positions.Position(
            symbol=symbol,
            side="LONG",
            entry_price=price,
            size_usdt=size_usdt,
            leverage=leverage,
            notional_usdt=notional,
            entry_ts_ms=ts,
            mode="dry",
        )
        positions.add(pos)
        journal.log(
            "open",
            {
                "symbol": symbol,
                "side": "LONG",
                "price": price,
                "size_usdt": size_usdt,
                "leverage": leverage,
                "notional_usdt": notional,
                "fee_usdt": fee,
                "mode": "dry",
            },
        )
        logger.info("[dry] OPEN LONG {} @ {} notional={}", symbol, price, notional)
        return FillResult(
            symbol=symbol, side="LONG", price=price, size_usdt=size_usdt,
            leverage=leverage, ts_ms=ts,
        )

    def close(self, symbol: str, exit_trigger: str = "signal_decay") -> FillResult:
        pos = positions.find(symbol)
        if pos is None:
            raise ValueError(f"no position to close for {symbol}")
        price = fetch_mark_price(symbol)
        ts = int(time.time() * 1000)

        pnl_usdt = (price - pos.entry_price) / pos.entry_price * pos.notional_usdt
        fee = pos.notional_usdt * TAKER_FEE
        net_pnl = pnl_usdt - fee * 2  # entry + exit fees

        positions.remove(symbol)
        journal.log(
            "close",
            {
                "symbol": symbol,
                "exit_trigger": exit_trigger,
                "exit_price": price,
                "entry_price": pos.entry_price,
                "gross_pnl_usdt": pnl_usdt,
                "fees_usdt": fee * 2,
                "net_pnl_usdt": net_pnl,
                "held_ms": ts - pos.entry_ts_ms,
                "mode": "dry",
            },
        )
        logger.info(
            "[dry] CLOSE {} @ {} (entry {}) pnl={:.2f}U trigger={}",
            symbol, price, pos.entry_price, net_pnl, exit_trigger,
        )
        return FillResult(
            symbol=symbol, side="CLOSE", price=price, size_usdt=pos.size_usdt,
            leverage=pos.leverage, ts_ms=ts,
        )
