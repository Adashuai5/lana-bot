#!/bin/zsh
# One full decision cycle: collect → ask Claude to decide → execute.
# Called by launchd every 30 min and by fast_scan on new surge signals.

export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

set -euo pipefail

PROJECT="/Users/ada/lana-bot"
cd "$PROJECT"

LOG="$PROJECT/logs/cycle.log"
PIDFILE="/tmp/lana-bot-cycle.pid"
mkdir -p "$PROJECT/logs" "$PROJECT/data/decisions"

# PID-file lock
if [[ -f "$PIDFILE" ]]; then
  old_pid=$(cat "$PIDFILE" 2>/dev/null || echo "")
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "$(date -Iseconds) cycle already running (PID $old_pid), skipping" >> "$LOG"
    exit 0
  fi
  rm -f "$PIDFILE"
fi
echo $$ > "$PIDFILE"
trap "rm -f $PIDFILE" EXIT INT TERM

{
  echo "=== cycle start $(date -Iseconds) ==="

  # 1. Collect fresh market data
  if ! /usr/local/bin/uv run python scripts/collect.py; then
    echo "WARN: collect.py failed (exit $?) — retrying in 30s"
    sleep 30
    /usr/local/bin/uv run python scripts/collect.py || \
      echo "WARN: collect.py retry also failed — proceeding with existing candidates"
  fi

  # 2. Decide whether to call Claude
  CANDIDATES_FILE="$PROJECT/data/candidates/latest.json"

  # 文件不存在 → 跳过
  if [[ ! -f "$CANDIDATES_FILE" ]]; then
    echo "SKIP: candidates file missing" >> "$LOG"

  # long 和 short 都为空 → 跳过
  elif grep -q '"candidates": \[\]' "$CANDIDATES_FILE" && \
       grep -q '"short_candidates": \[\]' "$CANDIDATES_FILE"; then

    echo "SKIP: no tradable candidates (long+short empty)" >> "$LOG"

  else
    # 候选数量过滤（避免低质量调用 Claude）
    COUNT=$(grep -o '"symbol"' "$CANDIDATES_FILE" | wc -l | tr -d ' ')

    if [[ "$COUNT" -lt 2 ]]; then
      echo "SKIP: too few candidates ($COUNT), skip Claude" >> "$LOG"
    else
      NODE_BIN="${NODE_BIN:-/usr/local/bin/node}"
      CLAUDE_BIN="${CLAUDE_BIN:-/usr/local/bin/claude}"

      if [[ -x "$NODE_BIN" && -x "$CLAUDE_BIN" ]]; then
        timeout 300 "$NODE_BIN" "$CLAUDE_BIN" -p "@CLAUDE.md run one decision cycle now" \
          --model claude-sonnet-4-6 \
          --permission-mode acceptEdits \
          --output-format text \
        || {
          ec=$?
          if [[ $ec -eq 124 ]]; then
            echo "WARN: claude timed out after 300s — killing and releasing lock"
          else
            echo "WARN: claude exited with code $ec (rate limit or error)"
          fi
        }
      else
        echo "WARN: node ($NODE_BIN) or claude ($CLAUDE_BIN) not found — skipping decision step"
      fi
    fi
  fi

  echo "=== cycle end $(date -Iseconds) ==="
} >> "$LOG" 2>&1
