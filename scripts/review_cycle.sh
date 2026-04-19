#!/bin/zsh
# 每周自动复盘：生成绩效统计 → 让 Claude 读取并调整策略参数。
# 由 launchd/com.lana-bot.review.plist 每周日凌晨 3 点触发。
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

set -euo pipefail

PROJECT="/Users/ada/lana-bot"
cd "$PROJECT"

LOG="$PROJECT/logs/review.log"
mkdir -p "$PROJECT/logs" "$PROJECT/data/reviews"

{
  echo "=== weekly review start $(date -Iseconds) ==="

  # 1. 生成绩效统计
  /usr/local/bin/uv run python scripts/review.py || {
    echo "ERROR: review.py failed (exit $?) — aborting review cycle"
    exit 1
  }

  # 2. Claude 读取统计并调整策略参数
  NODE_BIN="${NODE_BIN:-/usr/local/bin/node}"
  CLAUDE_BIN="${CLAUDE_BIN:-/usr/local/bin/claude}"
  if [[ -x "$NODE_BIN" && -x "$CLAUDE_BIN" ]]; then
    timeout 300 "$NODE_BIN" "$CLAUDE_BIN" -p "@CLAUDE.md 运行每周复盘" \
      --permission-mode acceptEdits \
      --output-format text \
    || {
      ec=$?
      if [[ $ec -eq 124 ]]; then
        echo "WARN: claude 复盘超时（300s）"
      else
        echo "WARN: claude 退出码 $ec"
      fi
    }
  else
    echo "WARN: node 或 claude 未找到，跳过策略调整"
  fi

  echo "=== weekly review end $(date -Iseconds) ==="
} >> "$LOG" 2>&1
