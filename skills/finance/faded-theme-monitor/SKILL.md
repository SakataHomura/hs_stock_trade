---
name: faded-theme-monitor
description: 退潮主线监控（每日17:30任务，板块每日流程第5环节）。把 fading/ended/被挤出但行业趋势仍在的主线纳入 fading-theme-watch 监控池持续跟踪，每日用板块指数K线（收盘vs MA20/MA60 + MA20斜率 + 20日涨幅 + 表格2上榜数）判定 trend_ok/trend_weak/trend_broken/revive_candidate，等待重启信号；结构性淘汰伪主线（无sectors板块）不入池。触发条件：cron 17:30；用户问"退潮主线/监控池/XX主线还会启动吗/XX主线情况"。
triggers:
  - "退潮主线监控"
  - "监控池"
  - "XX主线还会启动吗"
  - "退潮主线现在什么状态"
category: finance
---

# 退潮主线监控（每日17:30）

> 主线退潮 ≠ 永久结束。只要行业趋势还在（板块指数趋势未破坏），退潮主线保持监控，等待重新启动信号。

## 架构

```
fading/ended/被挤出主线 ──自动登记──▶ faded_theme_watch 监控池
                                          │ 每日17:30 faded_theme_monitor.py
                                          ▼
                              板块指数K线判据 + 表格2上榜数
                                          │
              ┌───────────────┬───────────┴───────────┐
              ▼               ▼                       ▼
         trend_ok/weak    trend_broken(3天)    revive_candidate
         → watching       → dropped(出池)      → reviving(次日确认→revived)
```

## 数据库（portfolio 库）

### 表1：faded_theme_watch（监控池主表）

```sql
CREATE TABLE faded_theme_watch (
    id INT AUTO_INCREMENT PRIMARY KEY,
    theme_name VARCHAR(100) NOT NULL UNIQUE,  -- 与 industry_themes.theme_name 一致
    related_sectors TEXT NOT NULL,            -- 主线 related_sectors JSON（含 sectors 板块代码）
    peak_count INT DEFAULT 0,
    peak_date DATE,
    fade_date DATE NOT NULL,                  -- 退潮/入池日期
    fade_reason VARCHAR(200),                 -- 状态机fading/ended / 数量上限挤出 / 评分降级
    watch_status ENUM('watching','reviving','revived','dropped') DEFAULT 'watching',
    watch_days INT DEFAULT 0,
    last_check_date DATE,
    last_signal_type VARCHAR(50),             -- ⚠️ 列名用 signal_type 后缀，signal 是 MySQL 保留字
    checkin_count INT DEFAULT 0,
    created_at/updated_at DATETIME
);
```

### 表2：faded_theme_watch_checkins（每日快照）

- theme_name, check_date, sector_codes, close_vs_ma20/ma60, ma20_slope, d20_pct, list_count, signal_type, note
- 用途：回看每日趋势、计算 trend_broken 连续天数、reviving 确认
- **幂等设计**：`UNIQUE KEY uk_theme_date (theme_name, check_date)` + `save_checkin` 用 `INSERT ... ON DUPLICATE KEY UPDATE`（同日重跑覆盖当日快照）；`update_status` 以 `last_check_date == check_date` 判重，同日重跑时 `watch_days`/`checkin_count` 不累加。若同日 cron+手动重跑插多行，`get_broken_streak` 会把同日 2 条 trend_broken 计为 2 天 → 连续破位虚高提前误杀出池

### 表3：sector_rank_daily（全A排行每日快照）

- 列（实测 `DESC`，勿按旧文案写 trade_date）：`id, rank_date(日期), code(板块代码), name(板块名), zdf(当日%), zdf_d5, zdf_d20, zdf_d60, zgb_ratio(涨股比), zgb_up, zgb_total, lb(量比), hsl(换手), rank_daily, rank_d20, created_at`——**日期列是 `rank_date` 不是 `trade_date`，代码列是 `code` 不是 `sector_code`**，手工核验查询用错列名报 ERROR 1054
- 数据源：腾讯 hy2 板块排行接口（124行业全量），17:30 随监控任务落库
- 用途：全A相对位次基准（B级产候选硬条件）+ 涨股比3日趋势（zgb_hist，历史值从本表取）+ 池外板块分析数据源（18:30 独立环节，2026-08-24 起）
- 补库：`--snapshot-only --date YYYY-MM-DD`（只拉排行落库，不跑监控判据，用于交易日回溯补数据）

## 脚本

`/home/agentuser/data/定时任务打包/scripts/faded_theme_monitor.py`

```bash
python3 scripts/faded_theme_monitor.py                  # 今日（自动拉全A排行落库）
python3 scripts/faded_theme_monitor.py --date 2026-08-21  # 指定日期（重跑）
python3 scripts/faded_theme_monitor.py --dry-run        # 只算不写库
python3 scripts/faded_theme_monitor.py --snapshot-only --date 2026-08-21  # 只补全A排行快照（修 sector_rank_daily 历史）
```

入口：`bash scripts/run_faded_theme_watch_cron.sh [YYYY-MM-DD]`（nohup 模式，输出 STARTED PID）

## 判据规则

### 板块级（每条 related_sectors 板块代码）
| 信号 | 条件 | 含义 |
|---|---|---|
| trend_ok | 收盘 ≥ MA20 | 趋势完好 |
| trend_weak | 收盘 < MA20 但 ≥ MA60 | 趋势走弱未破坏 |
| trend_broken | 收盘 < MA60 且 MA20 斜向下 | 趋势破坏 |
| 涨股比 | hy2 rank 接口 zgb 字段（涨/总）×100；**3日趋势**（均<40% = 启动期特征）；单日快照废弃 | 低位启动 vs 高位扩散 |
| RankD/RankD20 | 当日/20日全A涨幅位次（/124），来自 sector_rank_daily | 全A相对强弱 |
| 量比 lb | 当日量比（hy2 rank 接口） | 放量验证 |
| ⭐ | RankD≤25(前20%) **且 量比≥1.2 且 信号非 broken** | 行业级强信号（18:00 单独产候选） |

MA20 斜率：5日前 MA20 对比，±0.1% 内算 flat。20日涨幅 = 今收/20交易日收盘-1。收盘 < MA60 但 MA20 斜率未向下（up/flat）→ 归 trend_weak（走弱未破坏、可能修复中，不判死）。

> ⚠️ **3日涨股比**：报告与判据用 `50%→45%→38%均44%` 格式（hist=前3日涨股比，均=近3日均值，数据不足1天时 hist=[当日]）。单日涨股比快照已废弃——单日易受炒作脉冲干扰，3日趋势才反映板块内部真实跟涨状态。

### 主线级（聚合多个板块）
- revive_candidate：表格2上榜数 ≥ 3、任一板块 trend_ok、**且存在3日涨股比均值<40% 且 当日涨股比<60%（未扩散）的 trend_ok 板块**（涨股比 = 上涨个股数/板块总个股数，来自 hy2 rank 接口 zgb 字段；3日均<40% = 板块内多数个股未跟涨，低位启动特征；当日<60% = 当日未普涨扩散，排除单日反抽/脉冲，区别于高位扩散/反抽）
- trend_ok：≥50% 板块 trend_ok
- trend_broken：半数以上板块 trend_broken（**n=2 需两板块全 broken**、n=3 需≥2）且 无板块 trend_ok。与 trend_ok 的"≥半数"刻意不对称——破位是更严重的尾部风险，需更高证据标准，单板块破位不放大为主线破位
- 其余 trend_weak

> ⚠️ 涨股比约束：只有"趋势转好+上榜数达标"但板块涨股比已≥40%（普涨扩散/高位反抽）**或当日涨股比≥60%（单日普涨脉冲）**时**不触发** revive_candidate——高位反抽/脉冲不是真二次启动，避免追高误报。已扩散但趋势好的主线按 trend_ok 处理，继续 watching。

### 状态机（update_status）
| 当前 → 新信号 | 结果 |
|---|---|
| watching + revive_candidate | reviving（待次日确认） |
| reviving + revive_candidate 连续2天 | **revived** 转正 → 建议明日16:00推理重走三层过滤 |
| reviving + 非candidate | 回 watching（候选失败） |
| watching + trend_broken | 第1~2天 watching（记天数），**连续3天 → dropped 出池**（连续天数按相邻 checkin 交易日间距计，缺检日打断连续——断档不被误计为破位天数） |
| 其余 trend_ok/weak | watching |
### 自动登记（auto_register_faded，幂等）

- 扫描当日+近3日 `industry_themes` 中 status='fading'/'ended' 的主线（**读取 16:30 修正 + 场景门控后最终 status**——16:00 初判 ended 但 16:30 反向修正回 active/peaking 的主线不登记，避免把刚确认技术健康的主线误登记为退潮；R1 大跌日 ended 延迟确认降级 fading 的主线照常登记，监控判据自会判定是否真破位）
- **无 sectors 板块（related_sectors JSON 里 sectors 为空）→ 结构性伪主线（口含烟/3D打印/头盔）不入池**，打印 ⏭️ 跳过
- **F=0 逻辑否定不入池**：三维判定下 ended 分两类——T=0 技术破坏（板块趋势坏了但产业逻辑还在，入池等 trend_ok 复活）vs F=0 纯概念/逻辑否定（结构性淘汰，出监控体系）。现有"无 sectors"判断与 F=0 通常重合；如有 sectors 但 score_fund=0 的记录，跳过登记
- 有 sectors → INSERT 监控池，fade_reason 标注退潮原因（**P2 扩展枚举**，语义对齐设计 §8）：
  - `tech_fade（status，峰值N只）`：状态机 ended/fading = 技术退潮（**新登记默认值**，替代旧文案「状态机退潮/结束」）
  - `identity_drift（核心 sectors 漂移）`：P2 链身份判定旧链闭合（核心 sectors 交集<1/2 且连续 2 日确认）
  - `cap_exceed（族内挤出）`：同链族候选 >8 时族内末位被挤出（16:00 2.2 上限**按链族**计），当日无 DB 记录 → 由下方「族内挤出登记」DB 差集检测自动登记（跨链族永不相挤）
  - 旧行 fade_reason 值（「状态机fading退潮」等历史文案）**不修改**（向后兼容）
- ⚠️ **登记快照会口径漂移**：INSERT 的 related_sectors 只是登记那天主线表的历史快照；主线表换血（concepts/sectors 更新）后监控池不感知。实测 09-03 化工：监控池仍含煤炭开采/农化制品（08-21 煤化工时代），主线表已是 PTA 口径（化学原料/化学制品/炼化及贸易）→ 18:00 A级建池取成分股混入中煤/华阳。修复机制见下方「每日口径同步」

### 族内挤出登记（cap_exceed 差集检测）

`auto_register_faded` 在 fading/ended 扫描**之后**追加 DB 差集检测，兜住「配额性不写库（当日无记录）」盲区：

- A = **近3日窗口内「最近一条记录 status ∈ ('active','peaking')」** 的 `is_legacy=0` 主线名集合（SQL：子查询 `GROUP BY theme_name` 取 `MAX(theme_date)` → JOIN 回本表按该日过滤 status）；B = 当日有名集合；**差集 A−B = 族内挤出链**
  - ⚠️ **A 不能用「近3日有记录」**：`ended` 链 0 上榜按规则不再重复写记录 → 每天都落入差集造成误报（2026-10-12 实跑 `⚠️ 族内挤出链 26 条 > 10`，25 条是 10-09 已 ended 链）。改用「最近一条记录仍 active/peaking」后 A=18、A−B=0（零误报）
- 安全条件：`len(B)==0` → 当日 16:00 推理未跑/全失败 → 整段跳过（防误登记风暴）；已在池 → 跳过（幂等，重跑安全）；无 sectors 板块 → 跳过；单次 ≤10 条（超出打印告警）
- 写入：`fade_reason='cap_exceed（族内挤出，峰值N只）'`、fade_date=当日，peak/peak_date/related_sectors 取该链最近一条同名记录
- 背景：16:00 上限已改为**按链族（parent_theme）≤8**（设计 §3.4 / industry-themes-reasoning 2.2）——跨链族不互相挤出，常规日零触发；本检测兜住族内挤出（某族单日 ≥9 条候选）与「当日无记录但行业趋势仍在」的尾部场景

### 每日口径同步（sync_sectors_from_themes）
- **位置**：main() 中 auto_register_faded 之后、load_watch_list 之前每日执行
- **覆盖范围**：`watch_status IN ('watching','reviving','revived')` —— 与 18:00 fetch_faded_watch_leaders.py 读表范围一致，**revived 行 18:00 仍用于 A级建池，必须同步**（漏掉 = 化工类重灾区修不到）
- **规则**：
  - 主线表最近同名记录 ≤3 日 → 用最新 related_sectors 覆盖（换血/重启同步口径，JSON 含 concepts+sectors 整体替换）
  - 3 < 间隔 ≤ 5 日 → 保旧（保留登记快照），报告标注"口径陈旧"，暂不覆盖
  - 间隔 > 5 日或无记录 → 置 dropped 出池（主线表已彻底放弃该主题，旧快照无限期过时无意义；推企微 ❌）
- **输出**：变更进报告「### 口径同步」段（格式：`↻ 名称 口径同步 [旧板块] → [新板块]（主线表 日期 最新口径）`；丢弃 `❌ 名称 丢弃（...失联X日 > 5 日）`；3-5日保旧 `⏸ 名称 保旧（...）`）；口径一致静默不进报告
- `--dry-run` 只打印决策不写库（与 auto_register 同）
- ⚠️ **断档日卡在 active/peaking 的主线永远不会被自动登记**：某交易日断档（API 429/数据源缺失，industry_themes 无当日记录）后，断档前状态为 active/peaking 的主线没有 fading/ended 记录可扫 → 不入池、无退潮闭环、18:00 也不分析（"静默消失"）。实例：8-27 断档，有色 8-26 卡在 active，池内无有色；而医药靠 8-24 的 fading 记录（近3日窗口内）侥幸入池。**处置**：断档恢复后人工核对这些主线——若认为其行业趋势仍在，按有 sectors 的主线手动 `INSERT INTO faded_theme_watch`（fade_reason='断档补登记'）；否则接受其退出监控体系。**注**：断档日本身 `len(B)==0` 会跳过族内挤出差集检测（防风暴），此场景仍需人工；**非断档日的「当日无记录」已由差集检测自动兜住**

## 输出与推送
- 报告：`/home/agentuser/data/定时任务打包/data/faded_theme_watch_YYYYMMDD.md`（每条：图标+名称+信号+板块明细+处理后状态）
- 关键信号推送到企微"个股分析机器人"：revive_candidate（🚀重启发端）、revived（✅转正）、dropped（❌出池）
- cron 最终回复只发摘要+报告路径，不发全文

## 陷阱

0. **dry-run 必须无副作用**：`--dry-run` 不得写库（auto_register/update_status/save_checkin）、不得写报告文件、不得推送。旧版 dry-run 循环内 continue 但仍写报告文件，会覆盖当日真实 cron 产出的报告（实测踩坑：18:55 dry-run 覆盖 17:30 真实报告为空壳）。**报告重建**：用只读脚本调 extract_sectors/get_sector_kline/analyze_sector/theme_signal 重新生成当日报告，不改库状态（今日已 revived/watching 的状态保持原样，勿重跑真实模式造成重复 checkin）。

1. **板块K线必须用 proxy.finance.qq.com stockinfoquery**：`https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get?code=pt0180xxx&ktype=day&fqtype=qfq&limit=240&fromDate=...&toDate=...`，返回 `data.nodes[]`（date/open/last/high/low/volume/100）。**不要用 web.ifzq.gtimg.cn fqkline**——对 pt 板块代码只返回1天数据，无法算 MA。字节解码 utf-8 失败时回退 gbk。
1b. **板块成分股接口 getBoardRankList 参数必须对齐生产脚本**：实测手写 `?board_code=X&sort_type=priceRatio&direct=down&offset=0&count=600` 返回 **0 只成分股**，而生产 URL `fetch_trend.py fetch_sector_stocks`/`verify_theme_sectors.py` 的 `?board_code=X&sort_type=priceRatio&direct=down&_appver=7.3&count=200`（**无 offset、带 _appver**）正常返回。写临时校验/审计脚本复用时直接复制生产 URL，不要凭记忆重构造；板块成分用于 sectors↔成分交集校验时，交集为空 ≠ 残留（P4 子链 sectors 沿用宽链 superset 是设计使然），先确认该链是否有 theme_constituents 成分基线再下结论。
2. **`signal` 是 MySQL 保留字**：建表列名用 `signal_type`/`last_signal_type`，否则 ERROR 1064。
3. **DDL 部分失败会留旧列名**：`CREATE TABLE IF NOT EXISTS` 在 SQL 文件中途报错时，前面的表已建但列名是旧版。重跑"修正版"不会重建旧表（IF NOT EXISTS 跳过）→ 必须先 `DESC`/`SHOW CREATE TABLE` 核实实际列名，不一致用 `ALTER TABLE ... CHANGE COLUMN` 对齐，再跑脚本。
4. **pymysql 必须在模块顶部 import**：`get_db()` 内 import 会因其他函数直接引用 pymysql 报 NameError。
5. **--date 重跑日期一致性**：update_status/save_checkin 必须传参数化 check_date（=today 参数），不能写死 date.today()，否则指定旧日期重跑会写错日期。
6. **板块文件缺失必须中止**：当日 plate_YYYYMMDD.md 不存在 → 监控中止，无法取表格2上榜数（重启信号判据）。
7. **结构性伪主线不入池是特性**：ended 但无 sectors（口含烟/3D打印）或 score_fund=0（逻辑否定）跳过登记，不要手动 INSERT——它们没有真实行业趋势可言，监控无意义。T=0 技术破坏的 ended（有 sectors、F≥2）才应入池等复活。
8. **同日重跑自动幂等；换判据重跑必须回滚状态**：同日重复运行（cron+手动补跑）由 UNIQUE(theme_name,check_date)+ON DUPLICATE KEY UPDATE 保证 checkin 覆盖当日快照、watch_days/checkin_count 不累加。**同日幂等由三重机制保证**：① `save_checkin` 覆盖当日快照；② `update_status` 在 `is_repeat`（`last_check_date==check_date`）时**冻结状态机**（直接 `return prev_status`，不推进 `revive_candidate→revived` / `trend_broken→dropped` 迁移）、`watch_days`/`checkin_count` 不累加；③ `get_broken_streak(conn,name,today)` **排除今日快照行**（防当日自身被计入连续破位天数 → 避免「第2天」误判为「连续3天」）。但换判据后重跑除外：新判据落地后，若当天已按旧判据跑过，直接 `--date` 重跑会因旧状态（如 revived/watch_days）污染新判据。正确流程：① `UPDATE faded_theme_watch SET watch_status='watching', watch_days=0, checkin_count=0, last_check_date=NULL, last_signal_type=NULL`（按需限定 theme 范围）；② `DELETE FROM faded_theme_watch_checkins WHERE check_date='当日'`；③ 完整重跑（真实模式）。重跑后旧触发结论可能变成 watching——这是新判据的预期拦截效果（高位扩散反抽被挡），需向用户说明旧推送的"转正"结论作废。

9. **报告板块行禁嵌套括号**：板块行格式必须属性分号分隔、无嵌套括号：`航运港口(trend_ok; 涨股比50%→50%均50%; RankD7/124; RankD20 29/124; 量比1.2; ⭐)`。旧版曾写 `涨股比50%(均50%)` 内嵌括号 → 18:00 `load_theme_report` 正则 `([^()]+?)\(([^()]*)\)` 解析错乱（航运港口丢失 rank/lb/⭐、板块名带分号前缀）。旧格式（`trend_ok,涨股比50.0%`）仍兼容：单段含逗号时按逗号拆。
10. **Rank 字段取数必须用 `(\d+)/` 锚定**：`RankD20 29/124` 直接用 `re.search(r"\d+")` 会抓到字段名里的 20 → 应匹配 `/(\d+)/` 后的数字（rankd20=29）。先判 `RankD20` 前缀再判 `RankD`。
11. **低涨股比但位次不达标仍要留真实败因**：C级说明不能只有兜底文案。trend_ok 板块若涨股比≥40%（已扩散）或位次不达标，必须写入 reason_diffs 并逐板块列明（否则用户看到"位次不达标"但与板块实际 RankD17 矛盾）。
12. **18:00 fetch_faded_watch_leaders.py 日期参数无效会静默产出错误报告**：`sys.argv[1]` 无格式校验，任一非 YYYYMMDD 参数（如误传 `--help`/typo）都会：① 生成垃圾文件 `faded_watch_leaders_<参数>.md`（必须先删除）；② **更危险**：`load_theme_report(日期)` 解析不到当日 17:30 报告 → report_sectors 为空 → **trend_broken 板块跳过逻辑静默失效** → trend_broken 板块（如化学制药）成分股混入候选池，可能把真实"空仓"日翻成假"交易机会"（实测误跑出"石药创新 均线低吸"，正确日期跑则是空仓——差异根源就是报告解析失败没跳过化学制药）。**处理流程：删垃圾文件 → 用正确 YYYYMMDD 重跑 → 按下方核验清单确认结论，勿采信误跑输出。**

13. **18:00 `_parse_ratio_text` 解析涨股比时 `均X%` 污染 hist**：`re.findall(r"([\d.]+)%", text)` 把 `均9%`（或旧格式 `(均44.3%)`）的均值也抓进历史序列：`16%→4%→8%均9%` → hist=[16,4,8,9]，hist[-1]（当日涨股比）被均值顶替（当日=8 实为 9 的均值）且 hist 多出一位。下游凡依赖当日涨股比的判据（20日位次达标+当日≥40%、当日<60% 未扩散约束）都会取错值。提取时先 `re.sub(r"均[\d.]+%", "", text)` 再提取；`zgb3` 独立从 `均X%` 解析。

14. **17:30 报告主线行格式必须与 18:00 正则兼容**：`fetch_faded_watch_leaders.load_theme_report` 的主线行正则是 `^- .*?\*\*(.+?)\*\*.*?→ \`(\w+)\` 上榜数=(\d+)`——**信号值必须纯 `\w+`（反引号内禁中文/空格/括号），且必须有 `上榜数=(\d+)` 字段**。revived（转正）行也不例外：曾写成 ``→ `revived（转正，17:30 不复检）`` → 正则整体匹配失败 → 该主线（化工产业链）不被解析 → `report_sectors` 为空 → 18:00 误降 🅲️（A级建池失效）。正确格式与 watching 行完全一致：`- ✅ **化工产业链** → \`revived\` 上榜数=11 低位板块=无`（板块行照旧）。**改报告格式后必须先用解析器自证**：`python -c "import fetch_faded_watch_leaders as m; print(m.load_theme_report('YYYYMMDD').keys())"` 确认目标主线在解析结果中、sector_map 非空，再重跑 18:00 全流程——不要只靠肉眼读报告。
   - **跨日重跑 17:30 必须传 `--date`**：次日凌晨重跑昨日报告时默认 `date.today()` 会因当日板块文件不存在而中止（`plate_YYYYMMDD.md` 缺失检查），重跑用 `python faded_theme_monitor.py --date 前一日`。
   - **重跑写库幂等性**：`auto_register_faded` 有 existing 集合跳过、`save_rank_snapshot` 是 `ON DUPLICATE KEY UPDATE` UPSERT、`sync_sectors_from_themes` 仅变更时 UPDATE、`update_status` 同日冻结状态机、`get_broken_streak` 排除今日快照——格式修复类同日重跑可直接执行、无需清表；换判据重跑才需按陷阱8回滚状态。

## 候选升级方向（未实施待评估）

> 上传包 sector_tracker 追踪系统已判定停摆且用户决定不恢复任务，但其4个信号设计点值得吸收进本监控判据：

1. **历史基线对比（相对增量）**：revive_candidate 现用绝对条件（上榜≥3只+trend_ok），缺"此前低位"对比。建议加约束：上榜数从长期低位（持续≥5日<3只）后首次回升≥3只 → 才算复活候选，避免高位反复上榜与底部首次回升混同
2. **涨股比<40%启动期特征**：真二次启动=底部修复、前排先行多数未动（涨股比低）；高位反抽=指数已高换票上榜。建议 revive 叠加"涨股比仍<40% 或 板块指数20日涨幅低位"约束
3. **无信号超时出池**：现有出池仅 trend_broken 连续3天一条路，缺"趋势未破但上榜持续为0"的僵尸清理。建议加：上榜数连续N天为0（建议5天）→ 出池或降级
4. **revived 附龙头**：转正时直接带出板块内 RPS_20>80 + 流通市值≥300亿 的 Tier1 龙头候选，缩短信号→可执行标的距离（对齐 sector_tracker leaders 表设计）

## 下游：18:00 监控池机会分析（环节6，cron b016d38d5e03）

17:30 监控完成后，18:00 cron（b016d38d5e03「监控池主线龙头分析-每日18:00」）读 `faded_theme_watch` 池，按主线启动可能分级（A/B/C），对 A/B 级主线筛出**可交易机会**。

> **peaking 正常分级语义（三维状态机口径；含场景门控 R2 降速 peaking）**：industry_themes 的 **peaking = 热度退坡但趋势+逻辑完好的正常分级**（H=0 但 T≥2&F≥2，或 T+F≥4 下限保护），**不是退潮**——16:45 照常建池精筛。R2 场景门控（普涨/大跌日低位跳升 active → 降速 peaking）产生的 peaking 同理：**不是退潮**、不进监控池、16:45 照常进池（仅暂不确认强势）。peaking 主线**不会出现在本监控池**（池只登记 fading/ended 及被挤出主线），18:00 也不覆盖其个股机会（那是 16:45 龙头精筛的职责）。用户问"XX主线为什么不在监控池/18:00 报告里"时先查主线状态：active/peaking → 走 16:45 口径；fading/ended → 走本监控池。16:00 初判 ended 但 16:30 反向修正回 active/peaking 的主线同理**不登记**（技术健康已确认，非退潮）。

**交易机会三分类**（18:00 独立口径，替代 16:45 旧三层漏斗/回撤四要素）：
- 入口：`bash scripts/run_faded_watch_leaders_cron.sh [YYYYMMDD]`（nohup，补跑传交易日；**在 `scripts/` 目录内直接 `bash run_faded_watch_leaders_cron.sh`，勿再带 scripts/ 前缀**——实测踩坑 `bash scripts/run_...` from scripts/ → File not found）
- 逻辑：读监控池 → 主线启动可能分级 theme_launch_level → A/B级主线 sectors 板块取大市值成分股 top10（≥300亿，trend_broken 跳过）→ `evaluate_trade_setup` 交易机会三分类

**全A基准**：18:00 分级与候选不只依赖监控池内部信号，增加全A相对位次硬条件，消除"航运港口全A第7却被判C、银行全A倒数却判B"类误判：
- **① B级产候选加全A位次硬条件**：低位启动判定 = 存在 trend_ok 板块 + 3日涨股比均值<40% + **当日涨股比<60%（未扩散）** + **（当日位次≤62 或 20日位次≤62 且当日涨股比≥40%）**
- **② 趋势判定下沉行业级**：任一板块 ⭐（当日RankD≤25前20% + 量比≥1.2 且非broken）→ 主线直接 🅱️，**仅⭐板块单独产候选**（focus_codes，行业级强信号不下沉到弱板块）
- **③ 涨股比改3日趋势 + 当日<60%未扩散拦截**：见上（sector_rank_daily 历史累积，数据不足时用当日；当日普涨齐涨=反抽/脉冲，非低位启动）
- 分级：🅰️reviving/revived/revive_candidate 必分析；🅱️行业级强信号 或 watching+trend_ok+低位启动+位次达标 重点分析；🅲️trend_ok 但已扩散（3日均≥40% 或 当日≥60%）或位次不达标 / trend_weak 只列状态；🅲️说明会列出每个 trend_ok 板块的真实败因
- **个股口径（位置×涨幅联动，贴顶≠追高）**：硬杀=结构非真龙头/活跃股、市值<300亿、PE<0、MA20斜率≤0、跌破MA20(距MA20<-3%)、放量下跌(阴线且量比≥1.5)、天量(量比>2.0)、近3日涨停、20日跌幅>8%；分级=🟢均线低吸(距20日高点≤-3%，沿上升MA20介入) / 🟡平台启动(贴顶但20日涨幅<8%——没涨过贴顶=横盘待突破，非追高) / 🟠追高警示(20日涨幅≥8%又贴顶——真追高，只警示不主推)
- **为什么弃用"回撤买点四要素"**：原"回撤买点四要素"(20日涨幅≥8%+回撤3~15%)对监控池慢牛/低位启动主线系统性误杀——B级定级依据是"个股未跟涨"(涨股比<40%)，个股20日涨幅自然<8%，四要素必杀全池（实测银行15候选全灭、长期空仓不具备可用性）
- 产物：`data/faded_watch_leaders_YYYYMMDD.md`（每条 A/B 主线：交易机会候选表+一句话依据+🟠追高警示小节+剔除摘要；尾部「📌 交易机会汇总」），cron 回复只发摘要（每条主线最多2只）
- 同源去重：同一来源板块最多2只；每主线最多4只；保留数<2 时汇总表备注 ⚠️候选不足
- 手工触发：`cronjob run` job_id=b016d38d5e03

> **池外提示已拆分**：原18:00报告尾部的「🌐 池外提示」（全A当日涨幅 TOP10 行业标池内/池外 + 低20日位次后50%但涨股比≥50%的潜在轮动起点）已从 18:00 拆分为 **18:30 池外板块主线归纳 + 18:45 池外个股筛选** 两个独立环节，18:00 不再输出池外内容。见下方「18:30/18:45 池外板块分析」小节

### 空仓/候选结论核验清单（防"假空仓/假机会"）
> 报告说"无交易机会"或"有候选"时，先按此清单快速核验再上报，尤其手动补跑/重跑场景：
- [ ] 产物文件名 = 目标 YYYYMMDD，无多余垃圾文件（误传错误日期参数会生成 `faded_watch_leaders_<参数>.md`）
- [ ] 报告每条 A/B 主线含「跳过板块：XX（trend_broken…）」行——缺失 = 17:30 报告解析失败，板块过滤失效，结论不可信。**例外**：⭐ 单板块驱动的 B级主线（focus_codes）**无「跳过板块」行是正常的**——⭐板块本身 trend_ok 不跳、非⭐板块不建池无所谓跳不跳。此时改核验：`load_theme_report(日期)` 确认 ⭐ 板块 signal=trend_ok、ok=True（⭐板块 ok=False 时应跳过却建池=解析失败）
- [ ] **空池≠解析失败（实测 2026-09-14 光通信）**：A/B 主线若只渲染 `## N. 🅰️/🅱️ 主线（status / signal）` + `- ⚠️ 板块成分股获取失败或全部 <300亿`、而无 板块现状/启动可能/跳过板块 行——这是 **build_pool 全部板块被跳过**（如唯一板块 trend_broken）后 render 主循环 early-continue 的空池分支（render_theme 根本未调用），**不是** 17:30 报告解析失败。核验：`load_theme_report(日期)` 确认该主线 sector_map 各板块 ok=False（trend_broken）即安全（无成分股泄漏），勿按上条"缺失=解析失败"误报。此空池消息措辞会掩盖真实原因（板块被跳过 vs 成分股拉取失败），汇报时如实写"板块 trend_broken 被跳过 → 空仓"
- [ ] **空池变体②：剩余 trend_ok 板块成分股全 <300亿（实测 2026-09-24 AI应用产业链）**：sectors 里部分 trend_weak/trend_broken 被跳 + 剩余 trend_ok 板块成分股无一只 ≥300亿 → build_pool 空 → 同样渲染"成分股获取失败或全部 <300亿"。与变体①区别：sector_map 里 trend_ok 板块 ok=True 属正常（不是解析失败），空仓真实原因是**板块小市值本质**（出版 top 凤凰传媒210亿、互联网电商 top 国联股份141亿）。核验配方：`load_theme_report(日期)` 确认 trend_ok 板块 ok=True → `fsl.get_all_constituents(板块code)` 枚举市值 top5 确认全部 <300亿。汇报写"板块成分股全部 <300亿 → 空仓"；此场景不是信号否定，板块未来换血/市值达标后可再入池
- [ ] **非空池但候选全被硬杀 → "🚫 无交易机会"（实测 2026-10-13 农业三链）**：pool 建出（有 ≥300亿 成分股）但全部被 evaluate_trade_setup 硬杀 → render 走 `if not kept` 分支渲染「🚫 无交易机会 / 剔除 N 只：详见下方剔除摘要」+「> 🚫 剔除 N 只（≥300亿候选）：…」。与空池①②③的区别：**不是空池**（有 板块现状/启动可能 行），是真实候选被硬杀。核验配方（复现整条链）：`m.load_watch_themes()` → 逐条 `m.theme_launch_level(th, report_meta)`（确认 lv/focus_codes）→ `m.parse_sectors` + focus 过滤 → `await m.build_pool(sectors, sector_map, top_n=10, min_ltsz=300)`（确认池内股票及 ltsz）→ `await ftl.analyze_pool(pool)`（拿 structure/d20/ma20_slope）→ `await rtl.get_quote_batch(codes)` + `await rtl.get_kline_volumes(code, limit=65)` → `m.evaluate_trade_setup(st, klines, q)` 逐只确认 kills。本次实例：⭐ 农产品加工（pt01801012）23 只成分股仅中粮糖业(sh600737,348亿) ≥300亿，被「深跌无趋势」硬杀 → 空仓；汇报如实写"⭐板块 ≥300亿 仅 1 只且被 X 硬杀 → 无合格机会"
- [ ] **d20 索引口径 `[-20]` 不是 `[-21]`（实测 2026-10-13 中粮糖业）**：脚本 `fsl.calc_rps`/`ftl.analyze_pool` 计算 `d20=(close[-1]-close[-20])/close[-20]`（=19 个周期）。核验自行复算若用 `[-21]`（20 个周期）会得到不同值——区间内有单日跳空/冲高回落时两值可差数个百分点甚至翻转判级：中粮糖业 `[-20]`=**-10.3%**（触发「深跌无趋势」硬杀）vs `[-21]`=**-1.3%**（不触发），数据源同为 qfq 无矛盾，纯粹索引差一格。**核验 d20 必须与脚本同索引 `[-20]`**；`fsl.get_stock_kline(code, limit)` 返回**纯 float 收盘价列表**（无 date/volume，末位=最新），需看形态/日期得另拉原始 `nodes`（含 date/open/last/high/low/volume）
- [ ] **无🟠追高警示小节 = 正常（实测 2026-09-24）**：render_theme 仅在 refined 中存在 `verdict=="✅ 保留" and level=="追高警示"` 个股时渲染追高警示节（源码 `chase = [r for r in refined if r["verdict"] == "✅ 保留" and r["level"] == "追高警示"]`）。当日保留标的全为 🟢均线低吸/🟡平台启动（无"20日涨幅≥8%又贴顶"个股）时无此节属正常渲染结果，**勿当渲染遗漏 bug 误报**。核验：对比昨日/前日报告确认该节确有渲染机制（如 09-23 有深南电路/兴森科技/三环集团 3 只真龙头追高警示），今日无 = 今日无追高警示股；或 grep 源码 chase 条件确认渲染约束
- [ ] **⭐ focus_codes 兜底：候选来源 ≠ ⭐ 板块（实测 2026-09-14 电子材料）**：⭐ 板块名不在 DB related_sectors 里时（口径同步换血后常见——monitor 的 ⭐ 玻璃玻纤 已不在 related_sectors [元件,电子化学品Ⅱ,塑料,半导体]），脚本不强过滤（fetch_faded_watch_leaders.py focus_codes 兜底分支"板块名与成分板块名不一致：不强过滤避免空池"）→ 主线仍升 🅱️、报告/日志标"仅玻璃玻纤/单独产候选"，**但候选实际来自全部 related_sectors 板块**（09-14 候选=塑料东材科技+元件景旺电子/生益科技，无玻纤股）。核验：`load_theme_report` 确认 ⭐ 板块 ok=True 即可，候选来源与 ⭐ 不一致属预期设计，勿当 bug
- [ ] **⭐ focus_codes 兜底 + 口径同步换血 → 空池（实测 2026-10-09 化工产业链）**：⭐ 板块（焦炭Ⅱ）在 17:30 报告里 ⭐，但**当日口径同步已把 DB related_sectors 换血为仅 [化学原料]** → focus_codes=[焦炭Ⅱ] 不在 DB → 兜底分支回退全部 DB sectors → 唯一板块 化学原料 report signal=trend_weak 被 build_pool 跳过 → 空池渲染"成分股获取失败或全部 <300亿"。与 09-14 电子材料兜底差异：fallback 后仍无 trend_ok 板块可建池 → **真实空仓**（即使 ⭐ 板块还在 DB，焦炭Ⅱ成分股也无 ≥300亿 大票，结果同样空）。核验配方：查 DB `faded_theme_watch.related_sectors`（build_pool 实际用 = 同步后口径）vs `load_theme_report` sector_map（信号/⭐ 来源 = 同步前快照）对照，确认 ⭐ 板块确实不在 DB、DB 剩余板块 ok=False 即安全（非解析失败）；汇报如实写"⭐板块已不在 DB related_sectors（口径同步换血）+ 剩余板块 trend_weak 被跳过 → 空仓"，勿套用"成分股<300亿"措辞
- [ ] **上榜数? 口径（实测 2026-09-14 医药产业链）**：主线在 18:00 报告出现但 17:30 报告无对应小节（只出现在口径同步段/新登记当日无小节）→ 渲染 `上榜数?`、无 板块现状 行，分级走 DB last_signal_type（trend_weak→🅲️ 只跟踪）。属正常，勿当报告残缺
- [ ] 候选数对照：**市值必须用 `rtl.get_quote_batch(codes)` 补**（历史某日 `fsl.get_all_constituents` 返回的 ltsz 字段全为 0/缺失，直接用 `float(s.get('ltsz',0))` 枚举会得到 0 只 ≥300亿——主脚本 build_pool 内同样如此，排序过滤仅靠接口自带 ltsz 时需警惕；核验时先 get_all_constituents 拿成分股清单 → get_quote_batch 补市值 → 枚举 ≥300亿，与报告"保留N+剔除M"总和对齐）。⚠️ **ltsz 缺失非恒定**：2026-09-24 实测 `fsl.get_all_constituents` 直接返回真实流通市值（凤凰传媒210亿/中南传媒183亿/国联股份141亿），枚举可用；但为稳妥，涉及"≥300亿 边界/计数"时仍用 get_quote_batch 交叉验证
- [ ] **剔除N只 vs 剔除原因明细数量可能不一致**：`🚫 剔除 4 只：…超跌反弹(3只)；…弱势(1只)；MA20斜率(1只)` = 4只股票 5个 kill reason（同一只股票可命中多个硬杀条件：如藏格矿业=超跌反弹+MA20斜率-0.55%）。标题 N 是股票数、明细是 reason 分布，**勿当 bug 误报**
- [ ] 抽样复算：`ftl.analyze_pool` 看 structure（弱势=跌破MA20/趋势走弱），`rtl.get_quote_batch` 看 PE（负数=亏损），与剔除原因逐条对上。**数据口径差异容忍**：`rtl.get_kline_volumes(limit=30)` 自算的 d20/pb 与报告（ftl.analyze_pool 用 K线260）可有 ~2pp 差异（亚钾国际 -5.9% vs 报告 -3.4%），只要不影响分级判定（如 d20 都在 -8%~8% 区间外/内同侧）即核验通过，勿因小数点不一致误报。⚠️ **若差异远超 ~2pp 且逼近判据阈值，先查 d20 索引口径**——脚本用 `close[-20]`（19 周期），核验若用 `close[-21]` 会不同（见上方 d20 索引口径条，中粮糖业 -10.3% vs -1.3%）
- [ ] 验证脚本写临时 .py 文件运行，勿用 `python3 -c` 内联嵌套单引号（shell 引号破坏会 NameError 或静默改义，实测踩坑）
- [ ] 核验脚本接口坑：读模块必须 `import refine_theme_leaders as rtl`（get_quote_batch / get_kline_volumes）和 `import fetch_theme_leaders as ftl`（analyze_pool）——**不存在 realtime_data_lib**（No module named）；模块函数（含 `fetch_sector_leaders.get_all_constituents`/get_stock_kline、`rtl.get_quote_batch`/get_kline_volumes、`ftl.analyze_pool`）**都是 async 协程**，必须 `await` 或包在 `asyncio.run(main())` 里，直接同步调用只拿到 coroutine 对象（rtl/ftl：AttributeError: 'coroutine' object has no attribute 'get'；fsl：`TypeError: object of type 'coroutine' has no len()`，实测 2026-10-09）；`analyze_pool` 入参必须是 `[{"code":"sh600036","src":"板块"}, ...]` dict 列表，传纯字符串列表 `["sh600036"]` 会 TypeError（源码 st["code"] 取字符串下标）；**`rtl.get_quote_batch(codes)` 返回 `Dict[str, dict]`（key=股票代码，非 list）**——`for s in q:` 会迭代出字符串 key 报 `AttributeError: 'str' object has no attribute 'get'`（实测 2026-09-15），必须 `for k, v in q.items()`；返回 dict 字段名：`ltsz`（流通市值，亿元，勿再 /1e8）/`pe`（**字符串**）/`vol_ratio`（量比）/`turnover`（换手率）
- [ ] nohup 入口进程用 process 工具查不到（not_found 正常）——用 `ps -p <PID>` + `tail /tmp/faded_watch_leaders_<date>.out` + 产物文件 mtime 确认完成

> ⚠️ **与16:45主线龙头分析的区别**：16:45（cron 182cb93910e5）定位=全市场**覆盖面雷达**（浏览型，active 主线每板块 top10 大市值，MA10 四线分级+五层漏斗精筛，主升浪口径）；18:00 本任务是监控池内的**机会挖掘**（聚焦型，A/B级主线内找均线低吸/平台启动，慢牛/低位启动口径）。两环节口径独立，16:45 修订不会自动流入 18:00。

## 18:30/18:45 池外板块分析

**定位**：原18:00「④ 池外提示」从18:00移除，拆分为两个独立 cron 环节，负责发现「不在监控池、但正在启动的产业方向」：

- **18:30 主线归纳**（`outside_pool_themes.py`，cron 9a12fa59ae37，入口 `bash scripts/run_outside_pool_themes_cron.sh [YYYYMMDD]`）
  - 数据源：sector_rank_daily 最新快照（17:30落库）+ industry_themes 当日主线 + plate_YYYYMMDD.md 表格1/表格2
  - 维度A=当日涨幅TOP10（池外行业，标记池内/池外）；维度B=低20日位次（rank_d20>62）+ 涨股比≥50%（潜在轮动起点，**权重×2**）；b方案=板块统计上榜数+概念标签命中
  - 产业归属：**公司主营业务语义推导（v2，2026-10-09 实施，设计文档 `20261009_池外归并主营推导动态化设计.md`）**——不做"行业→链"静态查表，归属由主营事实语义推导：
    - **数据准备**（`prepare_pool_derivation.py`）：候选板块（维度A/B 同上）→ getBoardRankList 代表股（市值TOP5 ∪ 当日涨幅TOP3）→ jiankuang 主营（gsjj.yw 主营文本 + zysr 产品构成，缓存 `data/business_cache/` 7天TTL）→ `data/pool_derivation_input_YYYYMMDD.json`
    - **Agent 语义推理**（18:30 cron 第2步，规范 `/home/agentuser/data/设计文档/20261009_池外主营推导_推理规范.md`）：每股主营 → 链，命名空间 = industry_themes（当日/全历史）链名并集；evidence 必须引用主营原文；low 置信度不计票；无匹配 → 「新链:主营共性短名」+ new_chain 标注（次日 16:00 确认）；数据缺失/分散 → 独立候选（宁可漏报不误报）
    - **消费生成**（`outside_pool_themes.py --cluster-from <result>`）：derived_theme 同链合并 → 评分/成色/冲突校验/refill → `outside_pool_themes_*.md/json`（结构 18:45 兼容）
    - **实操经验（2026-10-09 v2 首日实测，必读）**：
      - 推理前先读昨日 `data/pool_derivation_result_<前一日>.json`：① 对齐"新链:"命名（**主题名原样保留前缀**，消费端不做剥离——报告/JSON 里就是 `新链:电池产业链`；历史有 `新链:公用事业产业链`/`新链:电力设备产业链` 超"≤6字"字面，勿按规范裁短）；② **重复候板块直接镜像昨日逐股推理**（能源金属/电池/农化制品成分股几乎不变：能源金属→有色金属产业链+博迁新材→电子材料、电池→新链:电池产业链+铜冠铜箔→电子材料、农化制品→化工产业链），保证跨日一致
      - 消费端解析（`consume_derivation_result`）：`_INDEP={None,"","独立","independent",...}`→独立候选（按板块名成簇）；板块 code ∈ 当日主线 related_sectors sectors → **absorbed**（主线优先）；**仅 derived 非空且≠主线名才标 derivation_divergence**——板块逐股有主题但聚合投票最高票=1票→derived=null 时（10-09 软件开发 投票1/1/1），被吸收显示"无分歧"是正常的，不是漏记
      - 同名冲突改名实测：多板块同 derived 且与当日主线同名 → `{同名}-{首差异板块名不含Ⅱ}链`（10-09: 贵金属+能源金属→`有色金属-贵金属链`、化学纤维+农化制品→`化工-化学纤维链`）；**多板块合并是过成色硬门槛的关键**，单板块新链（化妆品/黄金珠宝/旅游/酒店餐饮/动物保健）全被 ⚫过滤"单板块=标签现象"，非信号否定、勿误报
      - 结果 JSON 自检：date/snap_date 与输入一致 + 每板块含 code/name/rank_daily/rank_d20/zdf/zdf_d5/zdf_d20/zgb_ratio/lb/_source/_weight/derived_theme/vote/stocks 全部字段；rank 指标必须原样保留（deepcopy 输入）；stocks 条目 = 原输入 dict + theme/confidence/evidence/note 四字段；**107 股必须全部有推理条目（有缺失即退出，防静默漏推断）**
      - **theme_refill_candidates 列名是 `candidate_theme`（不是 theme_name）**，查询/校验用错列名报 ERROR 1054；唯一键 uk_sector(sector_code)；refill 是 sector 级、**与 track_level 解耦**——10-09 新链:电池产业链 报告级被过滤（单板块）但 refill 记录仍 confirmed(4日)，正常。实际列：`id/candidate_theme/sector_code/sector_name/mapped_theme/first_seen/last_seen/confirm_count/status/updated_at`——**日期列是 `last_seen`（不是 last_seen_date）、计数列是 `confirm_count`**，核验查询用错列名同样 ERROR 1054
      - **新链命名延续表（跨日对齐用，勿新造近义名）**：工程机械→`新链:机械设备产业链`、白色家电→`新链:家电产业链`（10-08）；电池→`新链:电池产业链`、饰品→`新链:黄金珠宝产业链`、化妆品→`新链:化妆品产业链`、酒店餐饮→`新链:酒店餐饮产业链`、旅游及景区→`新链:旅游产业链`、动物保健Ⅱ→`新链:动物保健产业链`（10-09）；风电设备→`新链:风电产业链`、白酒Ⅱ→`新链:白酒产业链`、摩托车及其他→`新链:摩托车产业链`、保险Ⅱ→`新链:保险产业链`、电视广播Ⅱ→`新链:广播电视产业链`（10-12）。注意都是「主营共性短名」，同一主题跨日必须同名（同一板块再现时直接复用上表名，勿写「工程机械产业链」等变体）
      - **宽泛主线 sectors 会把池外候选「空心化」（实测 2026-10-12）**：当日 5 条 AI应用-* 主线（影视内容/出版传媒/广告营销/游戏互娱/AI技术）`related_sectors` **共用同一组 6 个传媒板块**（影视院线/广告营销/数字媒体/出版/电视广播Ⅱ/游戏Ⅱ），主线优先把它们全吸收，加之交运-公路铁路链吸收铁路公路 → 池外只剩彼此孤立的单板块新链（保险/风电/家电/白酒/摩托车/机械设备）→ **全部单板块 ⚫过滤，"建议纳入监控追踪"为空**。这不是漏报/失败，是主线宽口径下的正常结果；汇报如实写"池外实质无新增可追踪主线（当日强势板块已被主线吸收）"，勿当解析失败
      - **吸收表 `主线归属` 列会显示最末一条同名主线（cosmetic，非 bug）**：多条主线共用同一 sectors 时 `mainline_code_map[code]` 按 `existing_themes` 迭代序 last-wins → 本次 6 个传媒板块的"主线归属"列全部显示 `AI应用-AI技术链`，不影响 absorbed/divergence 判定；divergence 记录以 `agent_theme_orig` 为准
      - **同日重跑 / 重复触发处理（实测 2026-10-12）**：调度器串行 + 补跑 backlog 会让同一 job 同日被触发多次（实测 9a12fa59ae37 18:34 已 `last_status=ok`，19:11 再次触发）。**先判断是否已跑**：查 `~/.hermes/cron/jobs.json` 该 job 的 `last_run_at`/`last_status` + `~/.hermes/sessions/session_cron_<jobid>_<YYYYMMDD>_<HHMMSS>.json`（有无当日更早会话）+ 产物 mtime。**重跑安全流程**：① `cp -a` 备份 `pool_derivation_input/result_*` + `outside_pool_themes_*.md/json`；② 跑 prepare，比对新旧 input 的 board/stock 集合——一致则**直接复用现有 result json**（skill 鼓励镜像推理保证一致，勿重造），不一致才需重推；③ 跑 consume（refill UPSERT 同日幂等，`confirm_count` 不 +1）；④ diff 新产物 vs 备份，确认只剩 cosmetic 差异（**核验指纹**：input 与产物先 `open(p,'rb').read()` 字节比对；JSON 深比对**排除 `generated_at`**——该字段每次重跑刷新，是唯一预期差异；DB 侧核 `theme_refill_candidates.confirm_count` 未累加）。**⑤ 推送决策（关键）**：同一 job 当日已成功投递过（`last_status=ok` 且 `last_delivery_error=null`）后，重复触发若产物与已投递版本**逐字节一致**（无任何新信号）→ **最终回复 `[SILENT]` 抑制投递**，避免向企微重复推送同一份报告——报告类 job 的「无新内容」恰等价于「重复触发」场景（实测 2026-10-12 第三次触发 20:14：准备脚本重跑后 input 与备份 byte-identical → 复用 result → 产物与 18:34/19:16 两次逐字节一致，仅 `generated_at` 不同，DB 幂等 → 判定 [SILENT]）；若存在实质变更（如 `pool_count` 因监控池新增而变）则正常出摘要。**已知 cosmetic 差异（非 bug，勿误报）**：`absorbed_boards`/「已归主线板块」表的「主线归属」列取决于 `existing_themes` 无 `ORDER BY` 的迭代序（last-wins），同日重跑可能由 `AI应用-AI技术链` 变为 `AI应用-游戏互娱链`；`pool_count` 随监控池当日新增而变化（17→18，因 17:30 补跑新增 1 板块）。二者均不影响 absorbed/divergence 判定。**DB 凭据**：池脚本用 `user='wish', password='<DB_PASSWORD>'`（用 `root` → ERROR 1045 Access denied），核验 `theme_refill_candidates` 表时用对
- **主线优先（设计 3.5，2026-10-09 实施）**：消费阶段先做「板块 ∈ 当日主线 sectors」剔除——候选板块已被当日主线任一主题收录 → **从池外候选移除**（主线成员本就属于主线，18:30 不重复输出），记入 JSON 顶层 `absorbed_boards`（带 `mainline_theme`）；若 Agent derived_theme 与主线不同 → `derivation_divergence=true` + `agent_theme_orig`（原推导链名）。报告末尾渲染「🔄 已归主线板块」小节（分歧记录供 16:00 复核）；被吸收板块不入 refill 回补池
  - **兜底（设计 3.3，2026-10-09 实施）**：`pool_derivation_result_*.json` 缺失（推理宕机/主营全失败）→ `run_outside_pool_themes_cron.sh` 不再退出，而用 input 生成「全部独立候选」result（顶层 `derivation_unavailable=true`）继续消费；报告头部标注「⚠️ 推导不可用：主营数据失败/推理环节异常，候选全部独立观察」，JSON 空 themes 正常（18:45 兼容）
- **归属只由主营语义推导判定（2026-10-09）**：静态映射已全部退役，不保留在代码或文档；无对照节
- **同名 sectors 冲突硬校验**（resolve_theme_conflict）保留：候选与当日主线同名 → sectors 一致复用（is_existing_theme=true）/不一致强制改名（差异 1~2 个 → `{同名}-{首差异板块名不含Ⅱ}链`；≥3 → `{同名}-池外链`），conflict=true 标注「※池外口径与主线同名不一致」（10-08 推导重放实证：航运港口+铁路公路 vs 当日交运产业链 → 交运-铁路公路链）
    - **单板块子链会被成色硬门槛过滤（板块数<2=标签现象）**：10-08 推导重放实证——电池产业链（评分11.7/成色10）、船舶海工产业链（成色10）均单板块 → filtered，18:45 不筛；多板块聚合才上主线（公用事业=燃气Ⅱ+电力 评分11.8 成色10 → main）
   - 评分 = n_dim_b×3.0 + n_dim_a×1.5 + 平均涨股比/20 + min(平均量比,3) + min(上榜数/5,3)；强度 ≥8 strong / ≥4 moderate / else weak（概念命中加分已随静态映射退役）
  - **成色评级 track_level（对齐16:00 industry-themes-reasoning 2.1 三层过滤；用户要求过滤小/无执行性主线）**：
    - 硬门槛（一票否决→filtered）：覆盖板块数<2（单板块=标签现象，非行业现象）
    - 成色评分 quality（5维×0~3，准入：quality≥9 → main 追踪；6~8 → watch 观察线仅报告；<6或硬门槛不过 → filtered 剔除）：产业纵深（板块数≥3=3/2=2）+ 普涨度（涨股比≥70=3/50~69=2/<50=1）+ 趋势持续性（20日涨幅≥8=3/0~8=2/<0=1）+ 资金确认（量比≥1.2或上榜≥5=3 / 量比≥1.0或上榜≥1=2 / 否则=1）+ 概念确认（固定1，概念加分已退役）
    - 效果：16条候选 → 5条主线级（有色/军工/新能源/金融/地产基建），11条单板块 moderate（冶钢原料/油服工程/计算机设备/轨交设备Ⅱ等）全部 ⚫过滤
  - 标记 is_pool（板块已在监控池？）与 is_existing_theme（已是当日industry_themes主线？），**track_level=main 且两者皆否的主线=「建议纳入监控追踪」**；报告含「🟡观察线（成色6-8暂不追踪）」与「⚫过滤掉的小/无执行性主线」两个独立小节说明
  - 产物：`data/outside_pool_themes_YYYYMMDD.md` + **`outside_pool_themes_YYYYMMDD.json`**（供18:45，含 track_level/quality 字段）
  - **待回补清单落库**：strong/moderate 池外候选（sector 级，非既有主线）→ `build_refill_candidates` → `process_refill_candidates` 幂等 upsert `theme_refill_candidates` 表（sector_code 同日重跑只同步映射不 +1；waiting 连续确认≥2 → confirmed；断档>10 天重置为 waiting；merged/reverted 由 16:00 管理只读）→ 渲染 `data/refill_candidates_YYYYMMDD.md`；confirmed 候选供次日 16:00 环节回补（09-04 已落 24 条 confirmed，首见 09-03）；**被主线优先吸收的板块（mainline_absorbed=true）不入回补池**（已是主线成员，无需回补）
- **18:45 个股筛选**（`outside_pool_leaders.py`，cron b2e9f9055bcf，入口 `bash scripts/run_outside_pool_leaders_cron.sh [YYYYMMDD]`）
  - 读18:30 JSON，**只筛 track_level=="main" 主线**（filtered/watch 已由18:30过滤，不产生个股噪声；老JSON无 track_level 字段时回退到 strong/moderate）；每板块 getBoardRankList top10（按当日涨幅）
  - 龙头=真龙头（MA20/MA60多头排列）按市值降序；候选=🟢贴线(距MA10≤4%)/🟡偏开(≤8%)/🟡回踩区(≤15%)；剔除=超跌反弹/BIAS6>15%/MA20乖离>25%/活跃股MA20斜率≤0/回撤>20%/20日涨幅≤0；市值<300亿跳过
  - 场景匹配：趋势启动🚀/趋势中继📈/高潮加速⚡/退潮反弹🔄/混沌期🌫️
  - 产物：`data/outside_pool_leaders_YYYYMMDD.md`
  - **18:45 空仓/候选核验清单（防"假空仓"，实测 2026-09-09 军工/有色双主线零候选核验）**：
    - [ ] **复现报告**：`import outside_pool_leaders as opl; opl.screen_candidates(theme_name, t['sectors'])` 返回 (leaders, candidates, dropped)，与报告"龙头N/候选N/剔除N"逐条对齐即核验通过。**模块自包含**（get_qt_quote/get_kline/analyze_stock/screen_candidates 均在本模块），勿去 import westock_data 或 fetch_theme_leaders（后者**无 get_stock_kline**，只有 async analyze_pool 且入参必须 `[{"code","src"},...]` dict 列表）
    - [ ] **抽查必须用板块实际拉到的名单**：`get_sector_leaders` 按**当日涨幅**取 top10（getBoardRankList sort_type=priceRatio&direct=down），**不是市值排序**。抽查板块大票（如能源金属拉的是融捷/西藏矿业/华友/赣锋/天齐…，**不含**紫金/洛阳钼业；军工主题板块是航天装备Ⅱ/军工电子Ⅱ/航空机场，**不含**中航沈飞/中航西飞=航空装备Ⅱ）若不在实际名单里，得出"应该出现却缺失"是**误报**。先 `opl.get_sector_leaders(code, 10)` 打印实际名单再抽
    - [ ] **ltsz 单位是亿元**：qt.gtimg.cn fields[44]（流通市值）直接就是亿元，**不要再 /1e8**（实测抽查脚本里 /1e8 显示"市值0亿"是自己写的展示 bug，把 1500亿 打成 0亿，勿据此怀疑市值过滤失效）
    - [ ] **场景标注口径**：`identify_scene(candidates+leaders, ...)` 只对**保留股**算场景——保留股为 0 时标 🌫️混沌期"无候选数据"（即使全池 100% 超跌反弹按工具E应判 🔄退潮反弹）。两者操作结论同为观望/不参与，**勿当 bug 误报**，可向用户注明"实为退潮反弹"
    - [ ] **dim_b 来源主线零候选是预期**：dim_b=低20日位次+涨股比≥50%（低位反抽），板块 zdf_d20 深负（-5%~-11%）+ rank_d20 垫底（100+/124）时，≥300亿 大票几乎必然全在 MA20 下方 → 全量超跌反弹剔除。检查 18:30 JSON 的板块 zdf_d20/rank_d20 即可确认空仓真实性
- **口径注意**：多板块主线按实际板块标注来源（sec_name_map，勿标成集群第1个板块）；18:45 依赖 18:30 JSON，链路断裂时如实报告
- 手工触发：`cronjob run` job_id=9a12fa59ae37（18:30）/ b2e9f9055bcf（18:45）

## 与 industry-themes-reasoning 的关系

- 16:00 推理产出 fading/ended 主线后，17:30 监控任务自动接管（无需推理主动登记）
- 16:00 推理报告可将退潮主线标注"已入监控池"
- revived 转正主线：在 industry-themes-reasoning 2.1 三层过滤中按新识别候选重走（爆发例外规则适用）