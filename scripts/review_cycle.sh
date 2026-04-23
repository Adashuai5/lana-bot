#!/bin/zsh
# 每日自动复盘：生成绩效统计 → 让 Claude 读取并调整策略参数。
# 由 launchd/com.lana-bot.review.plist 每天凌晨 3 点触发。
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

set -euo pipefail

PROJECT="/Users/ada/lana-bot"
cd "$PROJECT"

LOG="$PROJECT/logs/review.log"
REVIEW_DIR="$PROJECT/data/reviews"
TOML="$PROJECT/config/strategy.toml"
DATE=$(date +%Y-%m-%d)
REVIEW_MD="$REVIEW_DIR/${DATE}.md"

mkdir -p "$PROJECT/logs" "$REVIEW_DIR"

{
  echo "=== daily review start $(date -Iseconds) ==="

  # 1. 生成绩效统计
  /usr/local/bin/uv run python scripts/review.py || {
    echo "ERROR: review.py failed (exit $?) — aborting review cycle"
    exit 1
  }

  # 2. 快照 strategy.toml（复盘前）
  TOML_BEFORE=$(cat "$TOML")

  # 3. Claude 读取统计并调整策略参数
  CLAUDE_OUTPUT=""
  NODE_BIN="${NODE_BIN:-/usr/local/bin/node}"
  CLAUDE_BIN="${CLAUDE_BIN:-/usr/local/bin/claude}"
  if [[ -x "$NODE_BIN" && -x "$CLAUDE_BIN" ]]; then
    CLAUDE_OUTPUT=$(timeout 300 "$NODE_BIN" "$CLAUDE_BIN" -p "@CLAUDE.md 运行每日复盘" \
      --permission-mode acceptEdits \
      --output-format text 2>&1) || {
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

  # 4. 对比 strategy.toml 变化
  TOML_AFTER=$(cat "$TOML")
  DIFF=$(diff <(echo "$TOML_BEFORE") <(echo "$TOML_AFTER") || true)

  # 5. 写入每日复盘 md
  {
    echo "# 复盘记录 ${DATE}"
    echo ""
    echo "**时间**：$(date '+%Y-%m-%d %H:%M:%S')"
    echo ""

    # 绩效摘要（从 latest.json 读取关键数字）
    if [[ -f "$REVIEW_DIR/latest.json" ]]; then
      echo "## 今日绩效"
      /usr/local/bin/uv run python -c "
import json
with open('$REVIEW_DIR/latest.json') as f:
    s = json.load(f)
print(f\"- 交易笔数：{s.get('total_trades', 0)}\")
print(f\"- 胜率：{s.get('win_rate_pct', 0):.1f}%\")
print(f\"- 盈亏比：{s.get('profit_factor', 0):.2f}\")
print(f\"- 净盈亏：{s.get('net_pnl_usdt', 0):+.2f} U\")
print(f\"- 当前净值：{s.get('equity_usdt', 0):.2f} U\")
" 2>/dev/null || echo "（统计读取失败）"
      echo ""
    fi

    # 参数变化
    echo "## 参数变化"
    if [[ -z "$DIFF" ]]; then
      echo "无变化（参数保持不变）"
    else
      echo '```diff'
      echo "$DIFF"
      echo '```'
    fi
    echo ""

    # Claude 复盘摘要
    echo "## 复盘摘要"
    if [[ -f "$REVIEW_DIR/daily_notes.txt" ]]; then
      cat "$REVIEW_DIR/daily_notes.txt"
    else
      echo "（Claude 未生成摘要）"
    fi
  } > "$REVIEW_MD"

  echo "复盘记录已写入 $REVIEW_MD"
  echo "=== daily review end $(date -Iseconds) ==="
} >> "$LOG" 2>&1
