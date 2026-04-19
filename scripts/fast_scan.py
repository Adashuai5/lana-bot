"""Fast market scanner — runs every 5 minutes via launchd.

Pure Python, no Claude call. Watches for sudden surges (price + volume).
If a new signal appears and cooldown has passed, triggers a full cycle
(collect → claude decision). Writes state to data/fast_scan_state.json.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loguru import logger
from lana_bot.config import DATA_DIR, LOGS_DIR, strategy
from lana_bot.data.binance_futures import fetch_all_24h_tickers
from lana_bot.state.positions import list_positions

STATE_FILE   = DATA_DIR / "fast_scan_state.json"
TRIGGER_COOLDOWN_S = 1800   # 30 min between Claude triggers
SURGE_PCT    = 15.0          # price change threshold to consider "surge"
SURGE_VOL    = 20_000_000    # min volume for surge alert (20M USDT)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_trigger_ts": 0, "last_surge_symbols": []}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def _trigger_full_cycle() -> None:
    logger.info("fast_scan: triggering full cycle")
    subprocess.Popen(
        ["/bin/zsh", str(ROOT / "scripts" / "cycle.sh")],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    logger.remove()  # drop default stderr sink (launchd already captures stderr to file)
    logger.add(LOGS_DIR / "fast_scan.log", rotation="5 MB", retention="7 days")
    cfg = strategy()
    surge_pct = float(cfg.get("fast_scan", {}).get("surge_pct", SURGE_PCT))
    surge_vol = float(cfg.get("fast_scan", {}).get("surge_vol_usdt", SURGE_VOL))

    state = _load_state()
    held = {p.symbol for p in list_positions()}

    try:
        tickers = fetch_all_24h_tickers()
    except Exception as e:
        logger.warning("fast_scan: ticker fetch failed: {}", e)
        return 1

    short_surge_pct = float(cfg.get("fast_scan", {}).get("short_surge_pct", surge_pct))

    # Find surging symbols not already held.
    # Two detection paths:
    #   1. 24h net change >= surge_pct  (existing — catches clean pumps)
    #   2. gain_from_low >= short_surge_pct  (new — catches pumps that started from
    #      a high 24h base, making the net 24h change look flat or negative)
    surging = [
        t.symbol for t in tickers
        if (t.price_change_pct >= surge_pct or t.gain_from_low_pct >= short_surge_pct)
        and t.quote_volume >= surge_vol
        and t.symbol not in held
    ]

    now = time.time()
    prev_surging = set(state.get("last_surge_symbols", []))
    new_surging  = [s for s in surging if s not in prev_surging]

    state["last_surge_symbols"] = surging
    state["last_scan_ts"] = now
    state["surge_count"] = len(surging)
    state["new_signals"] = new_surging

    logger.info(
        "fast_scan: {} surging, {} new — cooldown_remain={:.0f}s",
        len(surging), len(new_surging),
        max(0, TRIGGER_COOLDOWN_S - (now - state["last_trigger_ts"])),
    )

    cooldown_ok = (now - state["last_trigger_ts"]) >= TRIGGER_COOLDOWN_S
    if new_surging and cooldown_ok:
        logger.info("fast_scan: NEW signals {} → triggering cycle", new_surging)
        state["last_trigger_ts"] = now
        state["last_trigger_reason"] = new_surging
        _trigger_full_cycle()
    elif new_surging:
        logger.info("fast_scan: new signals found but cooldown active, skipping trigger")
    else:
        logger.info("fast_scan: no new signals")

    _save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
