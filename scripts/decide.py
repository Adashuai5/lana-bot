"""Helper for humans (and Claude) to dump the current decision-cycle context.

Usage: python scripts/decide.py
  → prints candidates + positions + recent journal in a single human-readable
    blob, which is what Claude would read at the start of a cycle.

This script does NOT call the LLM. It exists so you can:
  - sanity-check what Claude will see
  - pipe the output into `claude -p` manually for one-off decisions
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lana_bot.config import DATA_DIR, strategy  # noqa: E402
from lana_bot.state import journal, positions  # noqa: E402


def main() -> int:
    latest = DATA_DIR / "candidates" / "latest.json"
    if not latest.exists():
        print("no candidates yet — run scripts/collect.py first", file=sys.stderr)
        return 1

    candidates = json.loads(latest.read_text())

    print("=== strategy ===")
    print(json.dumps(strategy(), indent=2))
    print("\n=== positions ===")
    print(json.dumps([p.__dict__ for p in positions.list_positions()], indent=2))
    print("\n=== candidates (top 20) ===")
    print(json.dumps(candidates, indent=2))
    print("\n=== recent journal (last 30) ===")
    for entry in journal.tail(30):
        print(json.dumps(entry))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
