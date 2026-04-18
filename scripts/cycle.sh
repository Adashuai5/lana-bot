#!/bin/zsh
# One full decision cycle: collect → ask Claude to decide → execute.
# Called by launchd every 30 min and by fast_scan on new surge signals.
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890

source ~/.zshrc
export PATH="/usr/local/bin:$PATH"

set -euo pipefail

PROJECT="/Users/ada/lana-bot"
cd "$PROJECT"

LOG="$PROJECT/logs/cycle.log"
LOCK="/tmp/lana-bot-cycle.lock"
mkdir -p "$PROJECT/logs" "$PROJECT/data/decisions"

# Prevent concurrent cycles (fast_scan + launchd can both trigger this)
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Iseconds) cycle already running, skipping" >> "$LOG"
  exit 0
fi

{
  echo "=== cycle start $(date -Iseconds) ==="

  # 1. Collect fresh market data
  /usr/local/bin/uv run python scripts/collect.py

  # 2. Ask Claude to decide. claude CLI must be on PATH; adjust if needed.
  # --dangerously-skip-permissions avoids interactive prompts in headless runs;
  # we lock down what it can do via .claude/settings.json (already restrictive).
  CLAUDE_BIN="${CLAUDE_BIN:-$(which claude || echo /usr/local/bin/claude)}"
  if [[ -x "$CLAUDE_BIN" ]]; then
    "$CLAUDE_BIN" -p "@CLAUDE.md run one decision cycle now" \
      --permission-mode acceptEdits \
      --output-format text || echo "claude exited non-zero (possibly rate limited)"
  else
    echo "WARN: claude CLI not found at $CLAUDE_BIN — skipping decision step"
  fi

  echo "=== cycle end $(date -Iseconds) ==="
} >> "$LOG" 2>&1
