#!/bin/zsh
# One full decision cycle: collect → ask Claude to decide → execute.
# Called by launchd every 30 min and by fast_scan on new surge signals.

export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

set -euo pipefail

# 动态定位脚本目录，避免写死 /Users/xxx 路径导致迁移失败。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 约定 cycle.sh 位于 <repo>/scripts，故上一级目录即仓库根。
PROJECT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT"

# 统一日志输出文件。
LOG="$PROJECT/logs/cycle.log"
# 进程互斥锁文件，防止并发执行多个 cycle。
PIDFILE="/tmp/lana-bot-cycle.pid"
# Claude 调用预算状态文件（冷却+日额度计数）。
CLAUDE_GUARD_FILE="$PROJECT/data/decisions/claude_guard.json"
# 确保日志目录和 guard 数据目录存在。
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
  # [TOKEN-SAVE] 目标：在调用 Claude 前尽量用本地规则过滤，减少无效大模型请求。
  CANDIDATES_FILE="$PROJECT/data/candidates/latest.json"

  # 文件不存在 → 跳过
  # [TOKEN-SAVE] 没有候选就不调用 Claude，直接省掉一次完整对话 token 消耗。
  if [[ ! -f "$CANDIDATES_FILE" ]]; then
    echo "SKIP: candidates file missing" >> "$LOG"

  # long 和 short 都为空 → 跳过
  # [TOKEN-SAVE] 候选为空时不进入模型决策，避免“空跑”消耗 token。
  elif python - "$CANDIDATES_FILE" <<'PY'
# 这里用 JSON 解析而非 grep，避免因空格/换行格式变化导致误判。
import json,sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        d = json.load(f)
    long_n = len(d.get("candidates", []))
    short_n = len(d.get("short_candidates", []))
    raise SystemExit(0 if (long_n == 0 and short_n == 0) else 1)
except Exception:
    raise SystemExit(1)
PY
  then

    echo "SKIP: no tradable candidates (long+short empty)" >> "$LOG"

  else
    # 候选数量过滤（避免低质量调用 Claude）
    # [TOKEN-SAVE] 候选总数 < 2 时，信息密度太低，直接跳过可减少低价值 token 开销。
    COUNT=$(python - "$CANDIDATES_FILE" <<'PY'
# 统计 long + short 总候选数；解析失败时返回 0 走保守跳过分支。
import json,sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        d = json.load(f)
    total = len(d.get("candidates", [])) + len(d.get("short_candidates", []))
    print(int(total))
except Exception:
    print(0)
PY
)

    if [[ "$COUNT" -lt 2 ]]; then
      echo "SKIP: too few candidates ($COUNT), skip Claude" >> "$LOG"
    else
      # 预算保护：
      # 1) 最小调用间隔（默认 45 分钟）
      # 2) 每日最大调用次数（默认 24 次，可通过环境变量调小）
      # [TOKEN-SAVE] 两层限流都是为了硬性控制 token 消耗上限，防止被定时+异动触发打满。
      CLAUDE_MIN_INTERVAL_S="${CLAUDE_MIN_INTERVAL_S:-2700}"
      CLAUDE_DAILY_MAX_CALLS="${CLAUDE_DAILY_MAX_CALLS:-24}"
      DAY_KEY=$(date -u +%F)
      NOW_TS=$(date +%s)
      # 默认值：guard 文件不存在或损坏时使用。
      LAST_CALL_TS=0
      LAST_DAY=""
      DAY_COUNT=0
      if [[ -f "$CLAUDE_GUARD_FILE" ]]; then
        GUARD_LINE=$(python - "$CLAUDE_GUARD_FILE" <<'PY'
# 单次读取 day_key/day_count/last_call_ts，减少多次解析的复杂度。
import json,sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        d = json.load(f)
    print(f"{int(d.get('last_call_ts', 0))}\t{d.get('day_key', '')}\t{int(d.get('day_count', 0))}")
except Exception:
    print("0\t\t0")
PY
)
        IFS=$'\t' read -r LAST_CALL_TS LAST_DAY DAY_COUNT <<< "$GUARD_LINE"
      fi

      if [[ "$LAST_DAY" != "$DAY_KEY" ]]; then
        # 切日后日计数重置，仅 last_call_ts 用于冷却判断。
        DAY_COUNT=0
      fi

      ELAPSED=$((NOW_TS - LAST_CALL_TS))
      if [[ "$ELAPSED" -lt "$CLAUDE_MIN_INTERVAL_S" ]]; then
        # [TOKEN-SAVE] 冷却中直接退出：阻断重复调用，避免短时间内连续消耗 token。
        REMAIN=$((CLAUDE_MIN_INTERVAL_S - ELAPSED))
        echo "SKIP: Claude cooldown active (${REMAIN}s left)" >> "$LOG"
        echo "=== cycle end $(date -Iseconds) ==="
        exit 0
      fi

      if [[ "$DAY_COUNT" -ge "$CLAUDE_DAILY_MAX_CALLS" ]]; then
        # [TOKEN-SAVE] 达到日上限直接退出：把 token 成本锁在可预期预算内。
        echo "SKIP: Claude daily budget reached ($DAY_COUNT/$CLAUDE_DAILY_MAX_CALLS for $DAY_KEY)" >> "$LOG"
        echo "=== cycle end $(date -Iseconds) ==="
        exit 0
      fi

      NODE_BIN="${NODE_BIN:-/usr/local/bin/node}"
      CLAUDE_BIN="${CLAUDE_BIN:-/usr/local/bin/claude}"

      if [[ -x "$NODE_BIN" && -x "$CLAUDE_BIN" ]]; then
        # 先假定成功；若 timeout/失败由 ec 覆盖。
        ec=0
        # 300s 超时保护，避免外部 CLI 卡住阻塞后续周期。
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
        if [[ $ec -eq 0 ]]; then
          # [TOKEN-SAVE] 仅成功调用才计数，避免失败重试误占预算，确保 token 统计准确。
          NEW_COUNT=$((DAY_COUNT + 1))
          python - "$CLAUDE_GUARD_FILE" "$DAY_KEY" "$NOW_TS" "$NEW_COUNT" <<'PY'
# 覆盖写入最新预算状态；字段保持最小集合便于脚本读取。
import json,sys
path, day_key, now_ts, day_count = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
with open(path, "w", encoding="utf-8") as f:
    json.dump({
        "day_key": day_key,
        "day_count": day_count,
        "last_call_ts": now_ts
    }, f)
PY
          echo "INFO: Claude budget usage today: $NEW_COUNT/$CLAUDE_DAILY_MAX_CALLS"
        fi
      else
        echo "WARN: node ($NODE_BIN) or claude ($CLAUDE_BIN) not found — skipping decision step"
      fi
    fi
  fi

  echo "=== cycle end $(date -Iseconds) ==="
} >> "$LOG" 2>&1
