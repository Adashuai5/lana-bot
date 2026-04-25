"""Strategy decision layer: apply hard rules to AI tendency, then call execute.py.

Decides open/skip based on:
  1. AI signal tendency (BUY_LIKELY in decision JSON)
  2. Hard thresholds from config/strategy.toml [strategy] section
  3. Candidate market data from candidates/latest.json

decide.py is the single decision authority. AI provides input; rules make the call.

Usage: python scripts/decide.py <decision_file>
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lana_bot.config import DATA_DIR, strategy  # noqa: E402
from lana_bot.state import journal  # noqa: E402


def _load_candidates() -> dict[str, dict]:
    try:
        payload = json.loads((DATA_DIR / "candidates" / "latest.json").read_text())
        all_cands = payload.get("candidates", []) + payload.get("short_candidates", [])
        return {c["symbol"]: c for c in all_cands}
    except Exception:  # noqa: BLE001
        return {}


def _check_rules(row: dict, rules: dict) -> str | None:
    """Return rejection reason string, or None if all rules pass."""
    pc = float(row.get("price_change_pct", 0))
    min_pc = float(rules.get("min_price_change_pct", 0))
    max_pc = float(rules.get("max_price_change_pct", 0))
    if min_pc > 0 and pc < min_pc:
        return f"price_change {pc:.1f}% < min {min_pc}%"
    if max_pc > 0 and pc > max_pc:
        return f"price_change {pc:.1f}% > max {max_pc}%"

    oi = float(row.get("oi_change_1h_pct", 0))
    min_oi = float(rules.get("min_oi_change_1h_pct", 0))
    if min_oi > 0 and oi < min_oi:
        return f"oi_1h {oi:.1f}% < min {min_oi}%"

    ph = float(row.get("pct_from_4h_high", 0))
    min_ph = float(rules.get("min_pct_from_4h_high", 0))
    if min_ph > 0 and ph < min_ph:
        return f"pct_from_4h_high {ph:.1f}% < min {min_ph}%"

    return None


def main(decision_path: Path) -> int:
    cfg = strategy()
    rules = cfg.get("strategy", {})
    decision = json.loads(decision_path.read_text())
    candidates = _load_candidates()

    final_open = []
    for item in decision.get("open", []):
        symbol = item["symbol"]
        row = candidates.get(symbol)

        if row is None:
            # Not in current candidates — pass through, execute.py will handle
            journal.log("strategy_gate", {"symbol": symbol, "blocked": False, "reason": "candidate_data_missing"})
            final_open.append(item)
            continue

        rejection = _check_rules(row, rules)
        if rejection:
            journal.log("strategy_gate", {"symbol": symbol, "blocked": True, "reason": rejection})
        else:
            journal.log("strategy_gate", {"symbol": symbol, "blocked": False})
            final_open.append(item)

    filtered = {**decision, "open": final_open}
    filtered_path = decision_path.parent / f"{decision_path.stem}_gated.json"
    filtered_path.write_text(json.dumps(filtered, ensure_ascii=False))

    uv_bin = shutil.which("uv") or "/usr/local/bin/uv"
    result = subprocess.run(
        [uv_bin, "run", "python", "scripts/execute.py", str(filtered_path)],
        cwd=Path(__file__).resolve().parents[1],
    )
    return result.returncode


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: decide.py <decision_file>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1])))
