#!/bin/zsh
# 一个完整的决策周期：收集数据 → 让Claude做决策 → 执行交易
# 每30分钟自动执行一次，出现暴涨信号时也会被快速扫描程序触发

# 代理配置：优先用环境变量的代理，本地开发默认用本机代理
export https_proxy="${HTTPS_PROXY:-http://127.0.0.1:7890}"
# 设置HTTPS代理，没有就用本机7890端口
export http_proxy="${HTTP_PROXY:-http://127.0.0.1:7890}"
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
# 添加系统命令路径，让脚本能找到所有依赖工具

set -euo pipefail
# 开启脚本严格模式
# 作用：出错立即停止、未定义变量报错、管道命令失败也终止，防止脚本乱执行

# 依赖工具路径：自动查找，找不到就用默认路径
UV_BIN="${UV_BIN:-$(command -v uv || echo /usr/local/bin/uv)}"
# 查找uv工具（Python环境管理器）路径
NODE_BIN="${NODE_BIN:-$(command -v node || echo /usr/local/bin/node)}"
# 查找node工具路径
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo /usr/local/bin/claude)}"
# 查找claude命令行工具路径

# 动态定位脚本目录，避免写死 /Users/xxx 路径导致迁移失败。
# 自动找脚本所在文件夹，不用写死用户名，换电脑也能用
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 获取当前cycle.sh脚本所在的文件夹路径
# 约定 cycle.sh 位于 <repo>/scripts，故上一级目录即仓库根。
# 约定脚本在scripts文件夹，上一级就是项目根目录
PROJECT="$(cd "$SCRIPT_DIR/.." && pwd)"
# 获取项目根目录路径
cd "$PROJECT"
# 切换工作目录到项目根目录

# 统一日志输出文件。
# 所有运行日志都存在这个文件里
LOG="$PROJECT/logs/cycle.log"
# 定义日志文件路径
# 进程互斥锁文件，防止并发执行多个 cycle。
# 防止同时跑多个脚本，避免重复交易/重复调用AI
PIDFILE="/tmp/lana-bot-cycle.pid"
# 定义进程锁文件路径
# Claude 调用预算状态文件（冷却+日额度计数）。
# 记录AI调用次数、冷却时间，控制Token消耗
CLAUDE_GUARD_FILE="$PROJECT/data/decisions/claude_guard.json"
# 定义AI调用统计文件路径
# 确保日志目录和 guard 数据目录存在。
# 自动创建文件夹，不存在就新建
mkdir -p "$PROJECT/logs" "$PROJECT/data/decisions"
# 创建日志和数据文件夹（-p=自动递归创建）

# 辅助函数：运行数据收集脚本，失败自动重试
run_collect() {
  # 定义函数run_collect
  local retries=2 delay=30
  # 设置重试2次，每次等待30秒
  for i in $(seq 1 $retries); do
    # 循环重试
    if "$UV_BIN" run python scripts/collect.py; then
      # 执行Python数据收集脚本
      return 0
      # 成功就退出函数
    fi
    echo "WARN: collect.py attempt $i/$retries failed"
    # 打印失败警告
    if [[ $i -lt $retries ]]; then sleep $delay; fi
    # 没到最大重试次数就等待30秒
  done
  return 1
  # 重试完都失败，返回错误
}

# 进程锁逻辑（防止脚本重复运行）
if [[ -f "$PIDFILE" ]]; then
  # 如果锁文件存在
  old_pid=$(cat "$PIDFILE" 2>/dev/null || echo "")
  # 读取旧的进程ID
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    # 检查旧进程是否还在运行
    echo "$(date -Iseconds) cycle already running (PID $old_pid), skipping" >> "$LOG"
    # 记录日志：已有运行中的脚本，跳过本次执行
    exit 0
    # 直接退出，不重复执行
  fi
  rm -f "$PIDFILE"
  # 旧进程已结束，删除锁文件
fi
echo $$ > "$PIDFILE"
# 把当前脚本进程ID写入锁文件
trap "rm -f $PIDFILE" EXIT INT TERM
# 脚本退出/中断时，自动删除锁文件（防止卡死）

{
  # 大括号内所有内容，输出全部写入日志文件
  echo "=== cycle start $(date -Iseconds) ==="
  # 打印日志：开始新的决策周期 + 时间

  # 第一步：收集最新市场数据（失败直接退出）
  if ! run_collect; then
    # 如果数据收集函数执行失败
    echo "ERROR: collect.py failed after retries — exiting without stale data" >> "$LOG"
    # 记录错误日志：数据收集失败
    echo "=== cycle end $(date -Iseconds) ==="
    # 打印周期结束日志
    exit 1
    # 脚本退出
  fi

  # 第二步：判断是否需要调用Claude AI
  # [TOKEN-SAVE] 目标：在调用 Claude 前尽量用本地规则过滤，减少无效大模型请求。
  # Token节省：本地先过滤，不浪费AI调用次数
  CANDIDATES_FILE="$PROJECT/data/candidates/latest.json"
  # 定义交易候选币文件路径

  # 文件不存在 → 跳过
  # [TOKEN-SAVE] 没有候选就不调用 Claude，直接省掉一次完整对话 token 消耗。
  if [[ ! -f "$CANDIDATES_FILE" ]]; then
    # 如果候选币文件不存在
    echo "SKIP: candidates file missing" >> "$LOG"
    # 记录日志：跳过，无候选文件

  # long 和 short 都为空 → 跳过
  # [TOKEN-SAVE] 候选为空时不进入模型决策，避免"空跑"消耗 token。
  # 执行Python代码，检查是否有可交易币种
  elif "$UV_BIN" run python - "$CANDIDATES_FILE" <<'PY'
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
    # 如果多空币种都为空
    echo "SKIP: no tradable candidates (long+short empty)" >> "$LOG"
    # 记录日志：跳过，无可交易币种

  else
    # 有可交易币种，继续执行
    # 候选数量过滤（避免低质量调用 Claude）
    # [TOKEN-SAVE] 候选总数 < 2 时，信息密度太低，直接跳过可减少低价值 token 开销。
    # 统计候选币总数量
    COUNT=$("$UV_BIN" run python - "$CANDIDATES_FILE" <<'PY'
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

    if [[ "$COUNT" -lt 1 ]]; then
      # 如果候选币数量小于1
      echo "SKIP: too few candidates ($COUNT), skip Claude" >> "$LOG"
      # 记录日志：候选太少，不调用AI
    else
      # 候选足够，准备调用AI
      # 预算保护：调用间隔和日上限均从 strategy.toml 的 cycle_minutes 派生。
      # 只需修改 strategy.toml 中的 cycle_minutes，频次自动跟随。
      CYCLE_MINUTES=$("$UV_BIN" run python - "$PROJECT/config/strategy.toml" <<'PY'
import tomllib, sys
with open(sys.argv[1], "rb") as f:
    cfg = tomllib.load(f)
print(int(cfg.get("cycle_minutes", 30)))
PY
)
      # 间隔 = cycle_minutes × 60 - 60s（比周期少 1 分钟，确保每次都能通过冷却检查）
      CLAUDE_MIN_INTERVAL_S="${CLAUDE_MIN_INTERVAL_S:-$(( CYCLE_MINUTES * 60 - 60 ))}"
      # 日上限 = 每天分钟数 / cycle_minutes（即每个周期都调用一次）
      CLAUDE_DAILY_MAX_CALLS="${CLAUDE_DAILY_MAX_CALLS:-$(( 24 * 60 / CYCLE_MINUTES ))}"
      DAY_KEY=$(date -u +%F)
      # 获取当前日期（UTC）
      NOW_TS=$(date +%s)
      # 获取当前时间戳
      # 默认值：guard 文件不存在或损坏时使用。
      LAST_CALL_TS=0
      LAST_DAY=""
      DAY_COUNT=0
      if [[ -f "$CLAUDE_GUARD_FILE" ]]; then
        # 如果AI统计文件存在
        # 读取AI调用统计数据
        GUARD_LINE=$("$UV_BIN" run python - "$CLAUDE_GUARD_FILE" <<'PY'
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
        # 解析统计数据：上次调用时间、日期、今日调用次数
      fi

      if [[ "$LAST_DAY" != "$DAY_KEY" ]]; then
        # 如果是新的一天
        # 切日后日计数重置，仅 last_call_ts 用于冷却判断。
        DAY_COUNT=0
        # 重置今日调用次数为0
      fi

      ELAPSED=$((NOW_TS - LAST_CALL_TS))
      # 计算距离上次调用AI的时间
      if [[ "$ELAPSED" -lt "$CLAUDE_MIN_INTERVAL_S" ]]; then
        # 如果还在冷却时间内
        # [TOKEN-SAVE] 冷却中直接退出：阻断重复调用，避免短时间内连续消耗 token。
        REMAIN=$((CLAUDE_MIN_INTERVAL_S - ELAPSED))
        echo "SKIP: Claude cooldown active (${REMAIN}s left)" >> "$LOG"
        # 记录日志：AI冷却中，跳过
        echo "=== cycle end $(date -Iseconds) ==="
        exit 0
      fi

      if [[ "$DAY_COUNT" -ge "$CLAUDE_DAILY_MAX_CALLS" ]]; then
        # 如果今日调用次数达到上限
        # [TOKEN-SAVE] 达到日上限直接退出：把 token 成本锁在可预期预算内。
        echo "SKIP: Claude daily budget reached ($DAY_COUNT/$CLAUDE_DAILY_MAX_CALLS for $DAY_KEY)" >> "$LOG"
        # 记录日志：今日AI次数用完
        echo "=== cycle end $(date -Iseconds) ==="
        exit 0
      fi

      if [[ -x "$NODE_BIN" && -x "$CLAUDE_BIN" ]]; then
        # 检查node和claude工具是否可用
        # 先假定成功；若 timeout/失败由 ec 覆盖。
        ec=0
        # 300s 超时保护，避免外部 CLI 卡住阻塞后续周期。
        timeout 300 "$NODE_BIN" "$CLAUDE_BIN" -p "@CLAUDE.md run one decision cycle now" \
          --model claude-haiku-4-5-20251001 \
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
        # 调用Claude AI执行交易决策，超时300秒自动终止
        if [[ $ec -eq 0 ]]; then
          # 如果AI调用成功
          # [TOKEN-SAVE] 仅成功调用才计数，避免失败重试误占预算，确保 token 统计准确。
          # 更新AI调用统计数据
          NEW_COUNT=$((DAY_COUNT + 1))
          "$UV_BIN" run python - "$CLAUDE_GUARD_FILE" "$DAY_KEY" "$NOW_TS" "$NEW_COUNT" <<'PY'
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
          # 打印今日AI使用次数
        fi
      else
        echo "WARN: node ($NODE_BIN) or claude ($CLAUDE_BIN) not found — skipping decision step"
        # 依赖工具缺失，跳过决策步骤
      fi
    fi
  fi

  # 条件复盘：每满5笔平仓触发一次
  {
    TRADES_STATE_FILE="/tmp/lana_last_review_trades"
    CURRENT_TRADES=$(grep -c '"event":"close"' "$PROJECT/data/journal.ndjson" 2>/dev/null || echo 0)

    if [[ ! -f "$TRADES_STATE_FILE" ]]; then
      echo "$CURRENT_TRADES" > "$TRADES_STATE_FILE"
      echo "INFO: 条件复盘状态初始化，当前平仓数=$CURRENT_TRADES"
    else
      LAST_TRADES=$(cat "$TRADES_STATE_FILE" 2>/dev/null || echo 0)
      DIFF_TRADES=$(( CURRENT_TRADES - LAST_TRADES ))
      echo "INFO: 条件复盘检查 — 当前=$CURRENT_TRADES 上次=$LAST_TRADES 差值=$DIFF_TRADES"
      if [[ "$DIFF_TRADES" -ge 5 ]]; then
        echo "INFO: 满5笔，触发条件复盘"
        echo "$CURRENT_TRADES" > "$TRADES_STATE_FILE"
        bash "$SCRIPT_DIR/review_cycle.sh" 5trades || echo "WARN: 条件复盘执行失败（不影响主流程）"
      fi
    fi
  } || true

  echo "=== cycle end $(date -Iseconds) ==="
  # 决策周期结束，打印日志
} >> "$LOG" 2>&1
# 所有输出（正常+错误）都追加写入日志文件
