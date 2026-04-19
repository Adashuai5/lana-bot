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
    is_stub = True

    def get_mark_price(self, symbol: str) -> float:
        return fetch_mark_price(symbol)

    def get_open_positions(self) -> list[dict]:
        return []  # no exchange to sync with in dry-run

    def open_long(self, symbol: str, size_usdt: float, leverage: int, max_stop_loss_usdt: float | None = None) -> FillResult:
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
            max_stop_loss_usdt=max_stop_loss_usdt,
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

    def open_short(self, symbol: str, size_usdt: float, leverage: int, max_stop_loss_usdt: float | None = None) -> FillResult:
        if positions.find(symbol) is not None:
            raise ValueError(f"already have position in {symbol}")
        price = fetch_mark_price(symbol)
        notional = size_usdt * leverage
        fee = notional * TAKER_FEE
        ts = int(time.time() * 1000)

        pos = positions.Position(
            symbol=symbol,
            side="SHORT",
            entry_price=price,
            size_usdt=size_usdt,
            leverage=leverage,
            notional_usdt=notional,
            entry_ts_ms=ts,
            mode="dry",
            max_stop_loss_usdt=max_stop_loss_usdt,
        )
        positions.add(pos)
        journal.log(
            "open",
            {
                "symbol": symbol,
                "side": "SHORT",
                "price": price,
                "size_usdt": size_usdt,
                "leverage": leverage,
                "notional_usdt": notional,
                "fee_usdt": fee,
                "mode": "dry",
            },
        )
        logger.info("[dry] OPEN SHORT {} @ {} notional={}", symbol, price, notional)
        return FillResult(
            symbol=symbol, side="SHORT", price=price, size_usdt=size_usdt,
            leverage=leverage, ts_ms=ts,
        )

    def close(self, symbol: str, exit_trigger: str = "signal_decay", fraction: float = 1.0) -> FillResult:
        pos = positions.find(symbol)
        if pos is None:
            raise ValueError(f"no position to close for {symbol}")
        price = fetch_mark_price(symbol)
        ts = int(time.time() * 1000)

        closed_notional = pos.notional_usdt * fraction
        price_move = (price - pos.entry_price) / pos.entry_price
        if pos.side == "SHORT":
            price_move = -price_move
        pnl_usdt = price_move * closed_notional
        fee = closed_notional * TAKER_FEE
        net_pnl = pnl_usdt - fee * 2  # proportional entry + exit fees

        if fraction >= 1.0:
            positions.remove(symbol)
        else:
            positions.reduce(symbol, fraction)

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
                **({"close_fraction": fraction} if fraction < 1.0 else {}),
            },
        )
        logger.info(
            "[dry] CLOSE {}{} @ {} (entry {}) pnl={:.2f}U trigger={}",
            symbol, f" {fraction:.0%}" if fraction < 1.0 else "",
            price, pos.entry_price, net_pnl, exit_trigger,
        )
        return FillResult(
            symbol=symbol, side="CLOSE", price=price, size_usdt=pos.size_usdt * fraction,
            leverage=pos.leverage, ts_ms=ts,
        )
