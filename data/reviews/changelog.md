# 策略参数变更记录

每次修改 config/strategy.toml 时追加，记录改了什么、为什么。

---

## 2026-04-20

**max_concurrent_positions**: 10 → 2
- 原因：日志显示同时开3仓导致聚集亏损（VANRY/ACE/SIREN/ARPA），40%×2=80%资金已足够，第3仓往往是凑数低质量信号

**min_pullback_from_high_pct**: 3.0 → 4.0
- 原因：多笔快速止损（GTC 24min、SIREN 32min）均为追顶入场，多留1%缓冲过滤

**monitor.py POLL_SECONDS**: 10 → 5
- 原因：hard_sl 触发延迟导致超出预期亏损（GTC -30U vs 预期-18U），5秒轮询将超出量减半

**[filters] fomo_gain_from_low_pct/fomo_atr_max_pct/fomo_oi_min_pct**（新增）
- 原因：增加 FOMO 路径：gain_from_low>=50% 且 ATR<15% 时 OI 门槛从5%降至2%，捕捉持续型泵（如 RAVE 类型）

**review 周期**: 每周日 → 每天凌晨3点
- 原因：交易频率低时7天数据太滞后，每日复盘参数调整更及时

---

## 2026-04-25（被 revert）

> ⚠️ 以下调整基于错误的 -82% 回撤判断（review.py max_drawdown 计算 bug），随后全部 revert。

**position_size_pct**: 0.40 → 0.15（已 revert → 0.40）
**min_24h_change_pct**: 10 → 15（已 revert → 10）
**min_pullback_from_high_pct**: 4.0 → 6.0（已 revert → 4.0）
**max_hold_seconds**: 21600 → 10800（已 revert → 21600）

---

## 2026-04-26（commit ecda1a6）

**min_pullback_from_high_pct**: 4.0 → 3.0
- 原因：添加观察层候选逻辑后放宽入场条件，捕捉更多候选；与 max_oi_gap_bars_4h 同时放宽

**max_oi_gap_bars_4h**: 3 → 5
- 原因：49根5m bar的4h窗口自然噪声偏多，3 bars 过严导致大量合理候选被过滤

**max_oi_step_volatility_pct**（新增）: 3.0
- 原因：原硬编码 1.5% 过于严格；3.0% 更符合实际 OI 波动噪声水平

---

## 2026-04-26（今日复盘）

**min_oi_change_1h_pct**: 5 → 7（aggregator + strategy 双层）
- 原因：近5笔交易胜率20%/profit_factor≈0.35，SOMI/RAYSOL 均为 OI 确认偏弱的入场，提高门槛减少噪音开仓

**min_pullback_from_high_pct**: 3.0 → 4.0（aggregator + strategy 双层）
- 原因：同上，入场回调不足是 hard_sl 主因；注意此值历史上曾到 6.0（过严）和 3.0（过松），4.0 为当前合理中值
