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
