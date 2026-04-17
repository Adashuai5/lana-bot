"""Config loader — reads config/strategy.toml and config/exchange.toml."""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


@lru_cache(maxsize=1)
def strategy() -> dict[str, Any]:
    return _load_toml(CONFIG_DIR / "strategy.toml")


@lru_cache(maxsize=1)
def exchange_keys() -> dict[str, Any]:
    path = CONFIG_DIR / "exchange.toml"
    if not path.exists():
        return {}
    return _load_toml(path)


def reload() -> None:
    strategy.cache_clear()
    exchange_keys.cache_clear()
