"""Collect market data + write candidates/{timestamp}.json.

Run every cycle via launchd (before the decide step).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lana_bot.config import DATA_DIR, LOGS_DIR, strategy  # noqa: E402
from lana_bot.data.aggregator import build_candidates  # noqa: E402
from lana_bot.data.binance_square import fetch_square_mentions  # noqa: E402

NETWORK_STATUS_FILE = DATA_DIR / "network_status.json"


def _check_ip() -> None:
    """Detect outbound IP change and persist to network_status.json."""
    try:
        with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=5) as r:
            current_ip = json.loads(r.read())["ip"]
    except Exception as e:
        logger.warning("ip check failed: {}", e)
        return

    prev: dict = {}
    if NETWORK_STATUS_FILE.exists():
        try:
            prev = json.loads(NETWORK_STATUS_FILE.read_text())
        except Exception:
            pass

    changed = prev.get("ip") not in (None, current_ip)
    if changed:
        logger.warning("outbound IP changed: {} → {}", prev.get("ip"), current_ip)

    NETWORK_STATUS_FILE.write_text(json.dumps({
        "ip": current_ip,
        "changed": changed,
        "prev_ip": prev.get("ip") if changed else None,
        "ts": int(time.time()),
    }))


def main() -> int:
    logger.add(LOGS_DIR / "collect.log", rotation="10 MB", retention="14 days")

    _check_ip()

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
