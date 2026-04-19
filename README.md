# Lana 交易机器人

一个用 AI（Claude）驱动的币安合约自动交易机器人。**每 30 分钟自动扫描市场、做出决策、模拟下单**，全程不需要你盯盘。

> 当前处于**模拟交易模式**，不会动真实资金，可以放心跑着观察。

---

## 它能干什么

- 自动扫描币安合约市场，寻找短期涨势强劲的小币
- AI 判断要不要买、要不要卖
- **自动复利**：仓位大小随账户净值自动增大，无需手动调整
- 自动止损保护（亏损超过保证金 50% 自动平仓）
- **每周自动复盘**：Claude 分析一周绩效，自动优化策略参数
- 网页面板实时查看持仓、盈亏、历史记录

---

## 快速启动

### 第一步：安装依赖

```bash
cd /Users/ada/lana-bot
uv sync
```

### 第二步：启动机器人（三个服务）

```bash
# 主决策循环（每 30 分钟）
launchctl load ~/Library/LaunchAgents/com.lanabot.cycle.plist

# 快速扫描（每 5 分钟）— 发现异动自动触发完整决策
launchctl load ~/Library/LaunchAgents/com.lanabot.fastscan.plist

# 每周自动复盘（每周日凌晨 3 点）
launchctl load ~/Library/LaunchAgents/com.lana-bot.review.plist
```

### 第三步：打开网页面板

```bash
uv run python scripts/dashboard.py
```

浏览器打开 → http://127.0.0.1:5001

**之后什么都不用管了。** 面板里可以看到机器人在干嘛、当前持仓盈亏、历史记录。

---

## 停止机器人

```bash
launchctl unload ~/Library/LaunchAgents/com.lanabot.cycle.plist
launchctl unload ~/Library/LaunchAgents/com.lanabot.fastscan.plist
launchctl unload ~/Library/LaunchAgents/com.lana-bot.review.plist
```

---

## 手动触发

```bash
# 手动触发一次完整决策周期
bash scripts/cycle.sh

# 手动触发复盘（不等周日）
bash scripts/review_cycle.sh

# 查看本周复盘摘要
cat data/reviews/weekly_notes.txt

# 查看本周绩效数据
cat data/reviews/latest.json
```

---

## 策略说明

### 核心流程（每 30 分钟）

1. **扫描市场** — 看哪些小币最近涨得猛、且有真实资金在流入
2. **AI 判断** — Claude 分析，决定买哪个、卖哪个，或者什么都不做
3. **模拟下单** — 记录买卖，不动真钱

**两速机制：**
- **快扫**（每2分钟）：纯Python扫描，发现异动（涨幅>15%+成交量>2000万U）立即触发完整决策
- **完整决策**（每30分钟）：Claude分析全部信号，触发后30分钟内不重复

### 自动复利机制

**仓位大小 = 当前账户净值 × 40%**（最低 5U，最高 200U）

每次开仓前，系统自动从历史交易记录计算当前净值，然后派生这次的仓位大小：

| 账户净值 | 每仓大小 | 名义价值（10倍杠杆） |
|---------|---------|---------------------|
| 50 U | 20 U | 200 U |
| 100 U | 40 U | 400 U |
| 200 U | 80 U | 800 U |

**所有风控参数同步缩放**，不需要你手动调整任何配置。

### 风险保护

| 保护措施 | 触发条件 | 说明 |
|----------|----------|------|
| 单仓止损 | 亏损超过保证金 50% | 自动平仓，不扩大损失 |
| 止盈保护 | 盈利达止损额 1.5 倍后回撤 15% | 锁住部分利润 |
| 超时平仓 | 持仓超 6 小时 | 避免长期套牢 |
| 日亏上限 | 当日亏损超净值 60% | 暂停开仓 |
| 最大持仓 | 同时最多 2 个 | 分散风险 |

### 每周自动复盘

每周日凌晨 3 点，系统自动：
1. 统计过去 7 天的胜率、盈亏比、持仓时长等指标
2. Claude 读取统计数据，在安全范围内自动调整策略参数（如筛选阈值、最大持仓时间）
3. 把做了什么调整、为什么写入 `data/reviews/weekly_notes.txt`

你只需要偶尔看一眼复盘摘要即可，不需要手动修改任何参数。

---

## 配置文件

一般情况下不需要改。确实要改时：

**`config/strategy.toml`** — 核心参数

```toml
live_trading = false            # 改成 true 才会动真钱，谨慎！
position_size_pct = 0.40        # 每仓占净值的比例（40%）
leverage = 10                   # 杠杆倍数
max_concurrent_positions = 2    # 最多几个仓位
```

**`config/exchange.toml`** — API 密钥（不要上传到 git）

```toml
[binance]
api_key = "..."
api_secret = "..."
proxy = "socks5://127.0.0.1:7890"
```

---

## 当前状态

| 功能 | 状态 |
|------|------|
| 市场扫描 | ✅ 正常 |
| AI 决策（Claude） | ✅ 正常 |
| 模拟交易 | ✅ 正常 |
| 自动复利仓位缩放 | ✅ 正常 |
| 止损保护 | ✅ 正常 |
| 每周自动复盘 | ✅ 就绪 |
| 网页面板 | ✅ 正常 |
| 实盘模式 | ⏸️ 关闭（`live_trading = false`） |

---

## 开启实盘（慎重）

确认模拟跑了一段时间、决策质量满意后再考虑：

1. 确保 `config/exchange.toml` 里的 API 密钥正确
2. 确认币安后台已绑定 IP 到 API key
3. 修改 `config/strategy.toml`：

```toml
live_trading = true
```

---

## 目录结构

```
config/
  strategy.toml              # 策略参数（自动缩放后一般无需手动修改）
  exchange.toml              # API 密钥（勿上传 git）
scripts/
  cycle.sh                   # 完整决策周期入口
  collect.py                 # 市场数据收集
  execute.py                 # 执行 AI 决策（自动计算仓位大小）
  monitor.py                 # 风险守护进程（每 5 秒轮询）
  fast_scan.py               # 快速异动扫描
  review.py                  # 生成周绩效统计
  review_cycle.sh            # 每周复盘入口
  dashboard.py               # 网页面板
src/lana_bot/
  equity.py                  # 净值计算 + 动态风控参数派生
  risk/                      # 风控引擎
  execution/                 # 交易所客户端
  state/                     # 持仓与日志
data/
  positions.json             # 当前持仓（唯一状态源）
  journal.ndjson             # 事件日志
  candidates/                # 候选币列表
  decisions/                 # AI 决策文件
  reviews/
    latest.json              # 最新周绩效统计
    weekly_notes.txt         # Claude 复盘摘要
logs/                        # 运行日志
```
