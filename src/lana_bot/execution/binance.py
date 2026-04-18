"""Binance USD-M futures live client.

Signing: HMAC-SHA256 over query string per
https://developers.binance.com/docs/derivatives/usds-margined-futures
"""
from __future__ import annotations

import hashlib
import hmac
import math
import time
import urllib.parse
from functools import lru_cache

import requests
from loguru import logger

from lana_bot.data.binance_futures import fetch_mark_price
from lana_bot.execution.base import FillResult
from lana_bot.state import journal, positions

FAPI_BASE = "https://fapi.binance.com"
TAKER_FEE = 0.0004


class BinanceFutures:
    name = "binance"
    is_stub = False

    def __init__(self, api_key: str, api_secret: str, proxy: str | None = None) -> None:
        self.api_key = api_key
        self._secret = api_secret.encode()
        self._session = requests.Session()
        self._session.headers["X-MBX-APIKEY"] = api_key
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}

    # ------------------------------------------------------------------ helpers

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        qs = urllib.parse.urlencode(params)
        params["signature"] = hmac.new(self._secret, qs.encode(), hashlib.sha256).hexdigest()
        return params

    def _post(self, path: str, params: dict) -> dict:
        r = self._session.post(
            f"{FAPI_BASE}{path}", params=self._sign(params), timeout=10
        )
        if not r.ok:
            raise RuntimeError(f"Binance {path} {r.status_code}: {r.text}")
        return r.json()

    @lru_cache(maxsize=256)
    def _step_size(self, symbol: str) -> float:
        """Return LOT_SIZE stepSize for symbol (cached per process)."""
        r = self._session.get(
            f"{FAPI_BASE}/fapi/v1/exchangeInfo", params={"symbol": symbol}, timeout=10
        )
        r.raise_for_status()
        for s in r.json()["symbols"]:
            if s["symbol"] == symbol:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        return float(f["stepSize"])
        raise ValueError(f"LOT_SIZE filter not found for {symbol}")

    def _round_qty(self, symbol: str, qty: float) -> float:
        step = self._step_size(symbol)
        precision = max(0, round(-math.log10(step)))
        return round(math.floor(qty / step) * step, precision)

    # ------------------------------------------------------------------ public

    def get_mark_price(self, symbol: str) -> float:
        return fetch_mark_price(symbol)

    def open_long(self, symbol: str, size_usdt: float, leverage: int) -> FillResult:
        if positions.find(symbol) is not None:
            raise ValueError(f"already have position in {symbol}")

        self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

        price = fetch_mark_price(symbol)
        notional = size_usdt * leverage
        qty = self._round_qty(symbol, notional / price)

        resp = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty,
        })

        fill_price = float(resp["avgPrice"]) if float(resp.get("avgPrice", 0)) else price
        ts = int(resp.get("updateTime") or time.time() * 1000)
        fee = notional * TAKER_FEE

        pos = positions.Position(
            symbol=symbol,
            side="LONG",
            entry_price=fill_price,
            size_usdt=size_usdt,
            leverage=leverage,
            notional_usdt=notional,
            entry_ts_ms=ts,
            mode="live",
        )
        positions.add(pos)
        journal.log("open", {
            "symbol": symbol,
            "side": "LONG",
            "price": fill_price,
            "size_usdt": size_usdt,
            "leverage": leverage,
            "notional_usdt": notional,
            "fee_usdt": fee,
            "mode": "live",
            "order_id": resp.get("orderId"),
        })
        logger.info("[live] OPEN LONG {} @ {} notional={}", symbol, fill_price, notional)
        return FillResult(
            symbol=symbol, side="LONG", price=fill_price,
            size_usdt=size_usdt, leverage=leverage, ts_ms=ts,
        )

    def open_short(self, symbol: str, size_usdt: float, leverage: int) -> FillResult:
        if positions.find(symbol) is not None:
            raise ValueError(f"already have position in {symbol}")

        self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

        price = fetch_mark_price(symbol)
        notional = size_usdt * leverage
        qty = self._round_qty(symbol, notional / price)

        resp = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qty,
        })

        fill_price = float(resp["avgPrice"]) if float(resp.get("avgPrice", 0)) else price
        ts = int(resp.get("updateTime") or time.time() * 1000)
        fee = notional * TAKER_FEE

        pos = positions.Position(
            symbol=symbol,
            side="SHORT",
            entry_price=fill_price,
            size_usdt=size_usdt,
            leverage=leverage,
            notional_usdt=notional,
            entry_ts_ms=ts,
            mode="live",
        )
        positions.add(pos)
        journal.log("open", {
            "symbol": symbol,
            "side": "SHORT",
            "price": fill_price,
            "size_usdt": size_usdt,
            "leverage": leverage,
            "notional_usdt": notional,
            "fee_usdt": fee,
            "mode": "live",
            "order_id": resp.get("orderId"),
        })
        logger.info("[live] OPEN SHORT {} @ {} notional={}", symbol, fill_price, notional)
        return FillResult(
            symbol=symbol, side="SHORT", price=fill_price,
            size_usdt=size_usdt, leverage=leverage, ts_ms=ts,
        )

    def close(self, symbol: str, exit_trigger: str = "signal_decay") -> FillResult:
        pos = positions.find(symbol)
        if pos is None:
            raise ValueError(f"no position to close for {symbol}")

        close_side = "BUY" if pos.side == "SHORT" else "SELL"
        resp = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "closePosition": "true",
        })

        price = fetch_mark_price(symbol)
        fill_price = float(resp["avgPrice"]) if float(resp.get("avgPrice", 0)) else price
        ts = int(resp.get("updateTime") or time.time() * 1000)

        price_move = (fill_price - pos.entry_price) / pos.entry_price
        if pos.side == "SHORT":
            price_move = -price_move
        pnl_usdt = price_move * pos.notional_usdt
        fee = pos.notional_usdt * TAKER_FEE
        net_pnl = pnl_usdt - fee * 2

        positions.remove(symbol)
        journal.log("close", {
            "symbol": symbol,
            "exit_trigger": exit_trigger,
            "exit_price": fill_price,
            "entry_price": pos.entry_price,
            "gross_pnl_usdt": pnl_usdt,
            "fees_usdt": fee * 2,
            "net_pnl_usdt": net_pnl,
            "held_ms": ts - pos.entry_ts_ms,
            "mode": "live",
            "order_id": resp.get("orderId"),
        })
        logger.info(
            "[live] CLOSE {} @ {} (entry {}) pnl={:.2f}U trigger={}",
            symbol, fill_price, pos.entry_price, net_pnl, exit_trigger,
        )
        return FillResult(
            symbol=symbol, side="CLOSE", price=fill_price,
            size_usdt=pos.size_usdt, leverage=pos.leverage, ts_ms=ts,
        )
