"""Unified risk + score state persisted as data/state.json.

Schema: {"_version": N, "risk": {...}, "scores": {...}}

All reads/writes go through load() and save(). save() uses fcntl.flock for
cross-process mutual exclusion and a version number for lost-update detection
(optimistic: version mismatch → retry ≤3 times).
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path

from lana_bot.config import DATA_DIR

STATE_FILE = DATA_DIR / "state.json"
_RISK_STATE_FILE = DATA_DIR / "risk_state.json"
_SYMBOL_SCORES_FILE = DATA_DIR / "symbol_scores.json"

_DEFAULT: dict = {"_version": 0, "risk": {}, "scores": {}}


def _validate(obj: object) -> dict:
    """Return a structurally valid state dict; repairs any missing/wrong-type fields."""
    if not isinstance(obj, dict):
        return {**_DEFAULT}
    return {
        "_version": int(obj.get("_version", 0)),
        "risk":     obj["risk"]    if isinstance(obj.get("risk"),    dict) else {},
        "scores":   obj["scores"]  if isinstance(obj.get("scores"),  dict) else {},
    }


def _try_load_json(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def load() -> dict:
    """Return current state. Repairs corrupt files; migrates old separate files on first run."""
    if not STATE_FILE.exists():
        risk = _try_load_json(_RISK_STATE_FILE)
        scores = _try_load_json(_SYMBOL_SCORES_FILE)
        state = _validate({"risk": risk, "scores": scores})
        save(state)
        return state

    try:
        raw = json.loads(STATE_FILE.read_text())
        validated = _validate(raw)
        if validated != raw:
            save(validated)
        return validated
    except Exception:  # noqa: BLE001
        return _validate({})


def save(state: dict) -> bool:
    """Atomic write with optimistic version check and cross-process file lock.

    Returns True on success, False after 3 failed retries (version conflict).
    Callers should log a warning if False is returned.
    """
    validated = _validate(state)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_FILE.with_suffix(".lock")

    for _attempt in range(3):
        lock_fd = lock_path.open("w")
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            on_disk = _validate(_try_load_json(STATE_FILE))
            disk_ver = on_disk["_version"]
            expected = validated.get("_version", disk_ver)
            if disk_ver != expected:
                # Another writer incremented the version between our load and save.
                validated["_version"] = disk_ver
                continue
            validated["_version"] = disk_ver + 1
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(validated, indent=2))
            tmp.replace(STATE_FILE)
            return True
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            lock_fd.close()
    return False
