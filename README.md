# lana-bot

AI 辅助的 Binance 合约交易机器人。Claude 每 30 分钟作为决策层运行一次——读取预排序的市场候选标的和当前持仓，然后输出一个由 Python 执行的决策文件。目前仅运行**模拟交易模式**。

---

## 架构

```
每 30 分钟（launchd）
  cycle.sh
    └─ collect.py          → data/candidates/latest.json
    └─ claude CLI          → 读取上下文，写入 data/decisions/{ts}.json
    └─ execute.py          → 通过交易所客户端执行开/平仓

持续运行的守护进程（launchd KeepAlive）
  monitor.py               → 每 10 秒轮询盈亏，触发止损 / 止盈 / 时间止损
```

**Claude 只负责判断** — 不直接调用交易所或抓取数据，所有 I/O 由 Python 处理。

---

## 策略

- **方向**：USDT 本位永续合约，仅做多，追短期动量
- **标的池**：24 小时涨幅强劲 + OI 上升的 Meme/小市值币种
- **仓位规模**：20 USDT 保证金 × 10 倍杠杆 = 200 USDT 名义价值
- **最大并发仓位**：3 个
- **硬止损**：每仓亏损 −10 USDT（由监控守护进程强制执行）
- **候选评分**：多维度加权评分（趋势斜率、OI 连续性、流动性分位），含 winsorization + z-score 异常值过滤

---

## 决策周期（Claude 的工作）

1. 读取 `data/candidates/latest.json` — 排名前 20 的候选标的
2. 读取 `data/positions.json` — 当前持仓
3. 读取 `data/journal.ndjson` 最后 30 行 — 近期事件
4. 写入 `data/decisions/{unix_ts}.json`：
   ```json
   {
     "open": [
       {
         "symbol": "RAVEUSDT",
         "size_usdt": 20,
         "reason": "OI 上涨 12%，价格上涨 40%"
       }
     ],
     "close": [{ "symbol": "FOOUSDT", "reason": "OI 背离，动量减弱" }],
     "skip_reason": null
   }
   ```
5. 对该文件运行 `execute.py`，然后停止。

---

## 风控规则

| 关卡                 | 阈值                        | 执行位置                     |
| -------------------- | --------------------------- | ---------------------------- |
| 每日最大已实现亏损   | −30 USDT                    | circuit_breaker.py（开仓前） |
| 组合未实现回撤上限   | −20 USDT                    | circuit_breaker.py（开仓前） |
| 板块集中度           | 单主题仓位 ≤ 60% 初始资金   | circuit_breaker.py（开仓前） |
| 每日最大开仓次数     | 12 次                       | circuit_breaker.py（开仓前） |
| 止损冷却时间         | 任意止损触发后 60 分钟      | circuit_breaker.py（开仓前） |
| 仓位上限             | 最多 3 个并发仓位           | execute.py                   |
| 硬止损               | 未实现亏损 −10 USDT         | monitor.py 守护进程          |
| 风险倍数止盈         | 盈利达 1.5× 风险，平仓 50%  | monitor.py 守护进程          |
| 追踪回撤保护         | 从峰值回撤 −3 USDT 平仓     | monitor.py 守护进程          |
| 最大持仓时间         | 6 小时（21600 秒）          | monitor.py 守护进程          |
| 市场机制门控         | BTC/ETH 趋势 + 宽度 + 波动率分位 | execute.py（默认禁用）  |

---

## 架构图

```
flowchart TD
    A[定时任务<br>（launchd / 手动）] --> B[cycle.sh]

    subgraph B [决策周期 cycle.sh]
        direction LR
        C[collect.py<br>采集市场数据] --> D[claude CLI<br>AI 生成决策]
        D --> E[execute.py<br>执行交易指令]
    end

    C --> F[(data/candidates/latest.json<br>候选标的快照)]
    D --> G[(data/decisions/*.json<br>AI 决策文件)]
    E --> H[(data/positions.json<br>持仓状态)]

    I[monitor.py<br>止损守护进程] -.->|实时监控| H

    E -.->|记录事件| J[(data/journal.ndjson<br>事件日志)]
    D -.->|读取上下文| J
    D -.->|读取上下文| F
    D -.->|读取上下文| H
```

## 项目结构

```
config/
  strategy.toml        # 所有可调参数（杠杆、过滤器、风控阈值）
  exchange.toml        # API 密钥 — 不提交到 git

scripts/
  collect.py           # 抓取 Ticker + OI，对候选标的排名，写入快照
  execute.py           # 读取决策 JSON，执行订单，记录事件
  monitor.py           # 止损守护进程（持续运行）
  cycle.sh             # 完整周期：collect → claude → execute
  decide.py            # 调试辅助：打印当前上下文但不生成决策
  dashboard.py         # Web 可视化面板（Flask，端口 5000）

templates/
  dashboard.html       # 面板前端（Vanilla JS，无框架）

src/lana_bot/
  config.py            # 加载/缓存 strategy.toml 和 exchange.toml
  data/
    binance_futures.py # Binance USD-M 公共 REST API（Ticker、OI、标记价格）
    aggregator.py      # 候选标的排名引擎
    binance_square.py  # Binance Square 热门话题爬虫（Playwright）
  state/
    positions.py       # positions.json 读写
    journal.py         # journal.ndjson 追加 + 读取末尾
  execution/
    base.py            # ExchangeClient 协议 + FillResult 数据类
    simulator.py       # 模拟客户端（以标记价格成交，手续费 0.04%）
    binance.py         # Binance 实盘客户端（存根，尚未实现）
    gate.py            # Gate.io 实盘客户端（存根，备用交易所）
  risk/
    stop_loss.py       # 未实现盈亏计算，should_stop_out()
    circuit_breaker.py # 账户级开仓前风控关卡（多维熔断）
    exit_rules.py      # 结构化出场逻辑（风险倍数 TP、追踪回撤、时间止损）
  data/
    market_regime.py   # 市场机制指标（BTC/ETH 趋势、宽度、波动率分位）+ 执行门控

data/
  candidates/          # 市场快照（latest.json 软链接 + 带时间戳文件）
  decisions/           # Claude 写入的决策文件
  positions.json       # 当前持仓（仅由 execute.py 写入）
  journal.ndjson       # 只追加的事件日志

launchd/
  com.lana-bot.cycle.plist    # 30 分钟周期调度器
  com.lana-bot.monitor.plist  # 止损守护进程（KeepAlive）

logs/
  collect.log / execute.log / monitor.log / cycle.log
```

---

## 安装与启动

```bash
# 安装依赖
uv sync

# 安装 Playwright 浏览器（用于 Binance Square 爬虫）
uv run playwright install chromium

# 复制并填写 API 密钥
cp config/exchange.toml.example config/exchange.toml
# 编辑 config/exchange.toml

# 手动运行一个完整周期
bash scripts/cycle.sh

# 或仅运行数据采集步骤
uv run python scripts/collect.py
# 然后手动使用 @CLAUDE.md 调用 claude

# 通过 launchd 启动守护进程（macOS）
launchctl load launchd/com.lana-bot.cycle.plist
launchctl load launchd/com.lana-bot.monitor.plist

# 启动 Web 面板
uv run python scripts/dashboard.py
# 访问 http://127.0.0.1:5000
```

---

## 配置说明（`config/strategy.toml`）

```toml
live_trading = false            # 仅在模拟验证通过后才改为 true
position_size_usdt = 20
leverage = 10
max_concurrent_positions = 3
cycle_minutes = 30

[filters]
min_24h_volume_usdt = 5_000_000
min_24h_change_pct = 10
min_oi_change_1h_pct = 5
top_n_candidates = 20

[risk]
max_daily_loss_usdt = 30        # 每日已实现亏损上限
max_unrealized_drawdown_usdt = 20 # 组合未实现回撤上限
max_sector_exposure_pct = 60    # 单主题仓位占初始资金比例上限
max_daily_opens = 12
stop_loss_cooldown_min = 60

[exit_rules]
risk_multiple_tp = 1.5          # 盈利达风险的 1.5 倍触发止盈
risk_multiple_tp_close_fraction = 0.5  # 止盈时平仓比例
trailing_drawdown_usdt = 3      # 从峰值回撤 3 USDT 触发追踪止损
max_hold_seconds = 21600        # 最大持仓时间 6 小时

[regime]
enabled = false                 # 是否启用市场机制分析
execute_gate_enabled = false    # 是否允许机制门控阻断/缩减开仓
reduce_size_multiplier = 0.5    # 缩减档位下的仓位比例
# BTC/ETH 4h 趋势阈值、市场宽度范围、波动率分位上限等详见 strategy.toml
```

---

## 数据流

所有状态均以纯 JSON 文件存储，无需数据库。

- `data/candidates/latest.json` — 由 collect.py 写入，由 Claude 读取
- `data/positions.json` — 仅由 execute.py 写入（模拟或实盘客户端）
- `data/journal.ndjson` — 只追加；每次开仓/平仓/决策/错误均记录于此
- `data/decisions/{ts}.json` — 由 Claude 写入，由 execute.py 消费

---

## 当前状态

| 模块                | 状态                             |
| ------------------- | -------------------------------- |
| 市场数据采集        | 正常运行                         |
| 候选标的排名        | 正常运行                         |
| 模拟交易模拟器      | 正常运行                         |
| 止损守护进程        | 正常运行                         |
| 熔断器              | 正常运行                         |
| Claude 决策周期     | 正常运行                         |
| Binance 实盘客户端  | 存根（尚未实现）                 |
| Gate.io 实盘客户端  | 存根（尚未实现）                 |
| Binance Square 爬虫 | 已实现（默认禁用）               |
| 实盘交易            | 已禁用（`live_trading = false`） |

---

## 依赖项

- `httpx` — Binance REST API 请求
- `loguru` — 带轮转的结构化日志
- `pydantic` — 数据验证
- `playwright` — 用于 Binance Square 爬取的无头浏览器
- `flask` — Web 面板服务
- `uv` — 依赖管理与运行器
