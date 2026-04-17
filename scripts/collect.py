"""Collect market data + write candidates/{timestamp}.json.

Run every cycle via launchd (before the decide step).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lana_bot.config import DATA_DIR, LOGS_DIR, strategy  # noqa: E402
from lana_bot.data.aggregator import build_candidates  # noqa: E402
from lana_bot.data.binance_square import fetch_square_mentions  # noqa: E402


def main() -> int:
    logger.add(LOGS_DIR / "collect.log", rotation="10 MB", retention="14 days")

    cfg = strategy()
    square_mentions: dict[str, int] = {}
    if cfg.get("square", {}).get("enabled"):
        try:
            square_mentions = fetch_square_mentions()
        except Exception as e:  # noqa: BLE001
            logger.warning("square scraper failed: {}", e)

    bundle = build_candidates(square_mentions=square_mentions)

    out_dir = DATA_DIR / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    out_path = out_dir / f"{ts}.json"
    out_path.write_text(json.dumps(bundle, indent=2))

    latest = out_dir / "latest.json"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(out_path.name)

    logger.info("wrote {} candidates to {}", bundle["count"], out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
