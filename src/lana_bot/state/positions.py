"""Position state persisted as data/positions.json.

Schema:
  {"positions": [{symbol, side, entry_price, size_usdt, leverage,
                  entry_ts_ms, notional_usdt, mode}], "updated_ts_ms": ...}
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from lana_bot.config import DATA_DIR

POSITIONS_FILE = DATA_DIR / "positions.json"


@dataclass
class Position:
    symbol: str
    side: str              # "LONG" | "SHORT"
    entry_price: float
    size_usdt: float       # collateral (margin) committed
    leverage: int
    notional_usdt: float   # size_usdt * leverage
    entry_ts_ms: int
    mode: str              # "dry" | "live"


def _read() -> dict:
    if not POSITIONS_FILE.exists():
        return {"positions": [], "updated_ts_ms": 0}
    return json.loads(POSITIONS_FILE.read_text())


def _write(state: dict) -> None:
    state["updated_ts_ms"] = int(time.time() * 1000)
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_FILE.write_text(json.dumps(state, indent=2))


def list_positions() -> list[Position]:
    return [Position(**p) for p in _read()["positions"]]


def find(symbol: str) -> Position | None:
    for p in list_positions():
        if p.symbol == symbol:
            return p
    return None


def add(pos: Position) -> None:
    state = _read()
    state["positions"].append(asdict(pos))
    _write(state)


def remove(symbol: str) -> Position | None:
    state = _read()
    for i, p in enumerate(state["positions"]):
        if p["symbol"] == symbol:
            removed = Position(**state["positions"].pop(i))
            _write(state)
            return removed
    return None
