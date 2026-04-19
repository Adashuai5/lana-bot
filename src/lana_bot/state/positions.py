"""Position state persisted as data/positions.json.

Schema:
  {"positions": [{symbol, side, entry_price, size_usdt, leverage,
                  entry_ts_ms, notional_usdt, mode, position_id}], "updated_ts_ms": ...}
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from lana_bot.config import DATA_DIR

POSITIONS_FILE = DATA_DIR / "positions.json"

_CORRUPTED = False


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
    max_stop_loss_usdt: float | None = None
    position_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def is_corrupted() -> bool:
    return _CORRUPTED


def _read() -> dict:
    global _CORRUPTED
    if not POSITIONS_FILE.exists():
        return {"positions": [], "updated_ts_ms": 0}
    try:
        return json.loads(POSITIONS_FILE.read_text())
    except Exception:  # noqa: BLE001
        _CORRUPTED = True
        from lana_bot.state import journal
        journal.log("warn", {"msg": "positions.json corrupted, fail-closed: no new opens allowed"})
        return {"positions": [], "updated_ts_ms": 0}


def _write(state: dict) -> None:
    state["updated_ts_ms"] = int(time.time() * 1000)
    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POSITIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(POSITIONS_FILE)


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


def reduce(symbol: str, fraction: float) -> Position | None:
    """Close `fraction` of a position in-place. Returns a Position sized to the closed portion."""
    state = _read()
    for p in state["positions"]:
        if p["symbol"] == symbol:
            closed = Position(
                symbol=p["symbol"],
                side=p["side"],
                entry_price=p["entry_price"],
                size_usdt=p["size_usdt"] * fraction,
                leverage=p["leverage"],
                notional_usdt=p["notional_usdt"] * fraction,
                entry_ts_ms=p["entry_ts_ms"],
                mode=p["mode"],
                position_id=p.get("position_id", str(uuid.uuid4())),
            )
            p["size_usdt"] = round(p["size_usdt"] * (1 - fraction), 8)
            p["notional_usdt"] = round(p["notional_usdt"] * (1 - fraction), 8)
            _write(state)
            return closed
    return None


def update_size(symbol: str, new_notional_usdt: float) -> None:
    """Correct position size from exchange sync. Does not affect leverage or margin."""
    state = _read()
    for p in state["positions"]:
        if p["symbol"] == symbol:
            p["notional_usdt"] = round(new_notional_usdt, 8)
            p["size_usdt"] = round(new_notional_usdt / p["leverage"], 8)
            _write(state)
            return
