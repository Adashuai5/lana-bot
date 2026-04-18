# Lana 交易机器人

一个用 AI（Claude）驱动的币安合约自动交易机器人。**每 30 分钟自动扫描市场、做出决策、模拟下单**，全程不需要你盯盘。

> 当前处于**模拟交易模式**，不会动真实资金，可以放心跑着观察。

---

## 它能干什么

- 自动扫描币安合约市场，寻找短期涨势强劲的小币
- AI 判断要不要买、要不要卖
- 自动止损保护（亏损超过 10U 自动平仓）
- 网页面板实时查看持仓、盈亏、历史记录

---

## 快速启动（三步搞定）

### 第一步：安装依赖

```bash
cd /Users/ada/lana-bot
uv sync
```

### 第二步：启动机器人

```bash
# 主决策循环（每 30 分钟）
launchctl load ~/Library/LaunchAgents/com.lanabot.cycle.plist

# 快速扫描（每 5 分钟）— 发现异动自动触发完整决策
launchctl load ~/Library/LaunchAgents/com.lanabot.fastscan.plist
```

### 第三步：打开网页面板

```bash
uv run python scripts/dashboard.py
```

然后浏览器打开 → http://127.0.0.1:5001

**之后什么都不用管了。** 面板里可以看到机器人在干嘛、当前持仓盈亏、历史记录。

---

## 停止机器人

```bash
launchctl unload ~/Library/LaunchAgents/com.lanabot.cycle.plist
launchctl unload ~/Library/LaunchAgents/com.lanabot.fastscan.plist
```

---

## 手动触发一次（不想等 30 分钟）

```bash
# 在面板里点「立即扫描」+「立即决策」按钮
# 或者命令行：
uv run python scripts/collect.py
claude -p "@CLAUDE.md run one decision cycle"
```

---

## 策略说明（用人话）

机器人每 30 分钟做一件事：

1. **扫描市场** — 看哪些小币最近涨得猛、且有真实资金在流入
2. **AI 判断** — Claude 分析，决定买哪个、卖哪个，或者什么都不做
3. **模拟下单** — 记录买卖，不动真钱

**两速机制：**
- **快扫**（每5分钟）：纯Python扫描，发现异动（涨幅>15%+成交量>2000万U）立即触发完整决策，不消耗Claude额度
- **完整决策**（每30分钟）：Claude分析全部信号，触发后30分钟内不重复触发

**买入条件（做多）：** 24 小时涨幅超 10%，且资金持续流入（不是虚假拉盘）

**卖出条件（做空）：** 暴涨超 30% 但资金开始撤退（轧空结束信号）

**风险保护：**

| 保护措施 | 触发条件 | 说明 |
|----------|----------|------|
| 单仓止损 | 亏损 -10U | 自动平仓，不扩大损失 |
| 止盈保护 | 盈利达 15U 后回撤 3U | 锁住部分利润 |
| 超时平仓 | 持仓超 6 小时 | 避免长期套牢 |
| 日亏上限 | 当日亏损 -30U | 暂停开仓 |
| 最大持仓 | 同时最多 3 个 | 不把所有钱压一块 |

**每仓规模：** 20U 保证金 × 10 倍杠杆 = 200U 名义价值

---

## 配置文件

只需关注两个文件：

**`config/strategy.toml`** — 调整策略参数（杠杆、止损幅度等）

```toml
live_trading = false   # 改成 true 才会动真钱，谨慎！
position_size_usdt = 20  # 每仓投入多少 U
leverage = 10            # 杠杆倍数
max_concurrent_positions = 3  # 最多几个仓位
```

**`config/exchange.toml`** — API 密钥和代理（不要上传到 git）

```toml
[binance]
api_key = "..."
api_secret = "..."
proxy = "socks5://127.0.0.1:7890"  # ClashX 代理，用于访问币安
```

---

## 当前状态

| 功能 | 状态 |
|------|------|
| 市场扫描 | ✅ 正常 |
| AI 决策（Claude） | ✅ 正常 |
| 模拟交易 | ✅ 正常 |
| 止损保护 | ✅ 正常（风控链路 v2：直接函数调用，不解析日志） |
| AI 决策硬约束层 | ✅ 正常（仓位上限 / 日亏上限在执行前强制过滤） |
| 网页面板 | ✅ 正常 |
| Binance 实盘交易 | ✅ 就绪（待开启） |
| 实盘模式 | ⏸️ 关闭（`live_trading = false`） |

---

## 开启实盘（慎重）

确认模拟跑了一段时间、决策质量满意后，再考虑开实盘：

1. 确保 `config/exchange.toml` 里的 API 密钥正确
2. 确认币安后台已绑定你的 IP 到 API key
3. 修改 `config/strategy.toml`：

```toml
live_trading = true
```

---

## 目录结构（了解即可）

```
config/                    # 配置文件
scripts/
  collect.py               # 市场数据收集（每 30 分钟）
  execute.py               # 执行 AI 决策（含硬约束过滤层）
  monitor.py               # 风险守护进程（每 10 秒轮询）
  fast_scan.py             # 快速异动扫描（每 5 分钟）
  dashboard.py             # 网页面板服务
src/lana_bot/
  risk/
    exit_engine.py         # 退出引擎：止损 / 止盈 / 超时（集中）
    risk_engine.py         # 风控统一入口：直接状态，不解析日志
    circuit_breaker.py     # 断路器：日亏上限 / 冷却期等
    exit_rules.py          # 出场规则参数与判断逻辑
    stop_loss.py           # 未实现 PnL 计算
  execution/               # 交易所客户端（实盘 / 模拟）
  state/                   # 持仓与日志
  data/                    # 市场数据 / 候选排序
data/
  positions.json           # 当前持仓（唯一状态源）
  risk_state.json          # 风控直接状态（止损时间戳等）
  journal.ndjson           # 事件日志（只写，不作为风控触发源）
  candidates/              # 候选币列表
  decisions/               # AI 决策文件
logs/                      # 运行日志
templates/                 # 网页面板
```
