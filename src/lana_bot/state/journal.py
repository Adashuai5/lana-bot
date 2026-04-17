"""Append-only NDJSON journal of every meaningful event.

Events include: decision, open, close, stop_loss_triggered, skip, error.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from lana_bot.config import DATA_DIR

JOURNAL_FILE = DATA_DIR / "journal.ndjson"


def log(event: str, payload: dict[str, Any]) -> None:
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts_ms": int(time.time() * 1000), "event": event, **payload}
    with JOURNAL_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def tail(n: int = 20) -> list[dict[str, Any]]:
    if not JOURNAL_FILE.exists():
        return []
    lines = JOURNAL_FILE.read_text().splitlines()[-n:]
    return [json.loads(line) for line in lines]
