#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主线龙头精筛股票回测（本周维度）
=================================
规则：当日 16:45 精筛报告（refine_theme_leaders_YYYYMMDD.md）保留的股票
      → 下一交易日开盘价买入（等权）
      → 持有至本周最后一个交易日收盘
      → 统计：本周收益（末收盘/买入开盘-1）、成功率（最终收益>0占比）、逐日收益序列

用法：
  python3 backtest_refine_leaders.py                          # 默认本周（20260824-20260828）
  python3 backtest_refine_leaders.py --dates 20260824,20260825 --end 2026-08-28

产物：{DATA_DIR}/backtest_refine_leaders_YYYYMMDD-YYYYMMDD.md
数据：腾讯 fqkline 前复权日K（含开盘/收盘），无第三方依赖
"""
import json
import os
import re
import sys
import time
import urllib.request

DATA_DIR = "/home/agentuser/data/定时任务打包/data"

# 2026-W35 交易日历（周一~周五）
WEEK_TRADE_DATES = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]

# 默认本周报告日 → 买入日（报告日下一交易日）
DEFAULT_REPORT_DAYS = ["20260824", "20260825", "20260826", "20260828"]
BUY_DATE_MAP = {
    "20260824": "2026-08-25",
    "20260825": "2026-08-26",
    "20260826": "2026-08-27",
    "20260827": "2026-08-28",
    "20260828": "2026-08-31",  # 周五报告次日为下周一，超出本周
}


def fetch_kline(code: str, limit: int = 30, retries: int = 3) -> list:
    """腾讯 fqkline 前复权日K，返回 [[date, open, close, high, low, vol], ...] 按日期升序"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={code},day,,,{limit},qfq"
    for attempt in range(retries):
        try:
            raw = urllib.request.urlopen(url, timeout=10).read().decode()
            data = json.loads(raw[raw.find("=") + 1:])
            node = data.get("data", {}).get(code, {})
            kl = node.get("qfqday") or node.get("day")
            if kl:
                return kl
        except Exception as e:
            if attempt == retries - 1:
                print(f"  ⚠️ {code} K线获取失败: {e}", file=sys.stderr)
        time.sleep(0.3)
    return []


def parse_refine(date_str: str):
    """解析精筛报告汇总表，返回 [{'theme','name','code'}, ...]"""
    path = os.path.join(DATA_DIR, f"refine_theme_leaders_{date_str}.md")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    # 汇总表：'# 📌 精筛汇总' 到 '**精筛逻辑' 之间的 markdown 表格
    m = re.search(r"# 📌 精筛汇总.*?\n(.*?)\n\*\*精筛逻辑", text, re.S)
    if not m:
        print(f"  ⚠️ {date_str} 未找到汇总表段", file=sys.stderr)
        return None
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if "股票名称" in line or re.match(r"^\|:?-", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3:
            rows.append({"theme": cells[0], "name": cells[1], "code": cells[2]})
    return rows


def build_kline_map(code: str) -> dict:
    """返回 {date: {'open': float, 'close': float}, ...}"""
    kl = fetch_kline(code)
    out = {}
    for k in kl:
        if len(k) < 3:
            continue
        try:
            out[k[0]] = {"open": float(k[1]), "close": float(k[2])}
        except (ValueError, TypeError):
            continue
    return out


def main():
    # ---- 参数 ----
    report_days = DEFAULT_REPORT_DAYS
    week_end = WEEK_TRADE_DATES[-1]
    if len(sys.argv) > 1 and sys.argv[1] == "--dates":
        report_days = sys.argv[2].split(",")
    if "--end" in sys.argv:
        week_end = sys.argv[sys.argv.index("--end") + 1]

    print("=== 主线龙头精筛回测 ===")
    print(f"报告日: {report_days} | 本周末: {week_end}")

    # ---- 解析报告 ----
    day_signals = {}   # report_day -> [{'theme','name','code'}]
    for rd in report_days:
        rows = parse_refine(rd)
        if rows is None:
            print(f"  ⚠️ {rd} 报告缺失，跳过")
            continue
        day_signals[rd] = rows
        print(f"  {rd}: {len(rows)} 只精筛股票")

    if not day_signals:
        print("无有效报告，退出")
        sys.exit(1)

    # ---- 拉K线（缓存） ----
    kline_cache = {}
    results = []   # 每条信号一条记录
    for rd, rows in day_signals.items():
        buy_date = BUY_DATE_MAP.get(rd)
        if buy_date and buy_date > week_end:
            print(f"  ⚠️ {rd} 报告买入日 {buy_date} 超出本周（{week_end}），不计入本周回测")
            continue
        for sig in rows:
            code = sig["code"]
            if code not in kline_cache:
                kline_cache[code] = build_kline_map(code)
            km = kline_cache[code]
            if not km or buy_date not in km:
                print(f"  ⚠️ {sig['name']}({code}) 买入日 {buy_date} 无K线数据，跳过")
                continue
            open_p = km[buy_date]["open"]
            # 逐日收盘（买入日 → 本周末之间有交易的日期）
            daily = []
            for d in WEEK_TRADE_DATES:
                if WEEK_TRADE_DATES.index(buy_date) <= WEEK_TRADE_DATES.index(d) <= WEEK_TRADE_DATES.index(week_end) and d in km:
                    daily.append({"date": d, "close": km[d]["close"]})
            if not daily:
                continue
            final_close = daily[-1]["close"]
            final_ret = (final_close / open_p - 1) * 100
            results.append({
                "report_day": rd, "buy_date": buy_date, "theme": sig["theme"],
                "name": sig["name"], "code": code,
                "open": open_p, "daily": daily, "final_ret": final_ret,
                "success": final_ret > 0,
            })

    # ---- 汇总 ----
    n = len(results)
    succ = sum(1 for r in results if r["success"])
    avg_ret = sum(r["final_ret"] for r in results) / n if n else 0
    med_ret = sorted(r["final_ret"] for r in results)[n // 2] if n else 0

    # 按报告日分组（批次等权收益）
    by_day = {}
    for r in results:
        by_day.setdefault(r["report_day"], []).append(r)

    # 逐日组合收益（各批次：买入日→每天收盘 等权平均）
    day_series = {}
    for rd, lst in by_day.items():
        for r in lst:
            for d in r["daily"]:
                ret = (d["close"] / r["open"] - 1) * 100
                day_series.setdefault(rd, {}).setdefault(d["date"], []).append(ret)

    # 大盘基准（上证指数，买入日起）
    sh = build_kline_map("sh000001")
    sh_ref = {}
    if sh:
        for dt, v in sh.items():
            if week_end >= dt:
                sh_ref[dt] = v["close"]

    # ---- 输出 MD ----
    fname = f"backtest_refine_leaders_{report_days[0]}-{report_days[-1]}.md"
    path = os.path.join(DATA_DIR, fname)
    lines = []
    lines.append(f"# 📊 主线龙头精筛回测（2026-W35）")
    lines.append("")
    lines.append(f"> 回测口径：当日 16:45 精筛报告保留股票 → **次日开盘买入**（等权）→ 持有至本周最后交易日 **{week_end}** 收盘")
    lines.append(f"> 成功率 = 最终收益(末收盘/买入开盘-1) > 0 的个股占比；收益均为前复权口径")
    lines.append(f"> 报告日 {', '.join(report_days)}（8/27 报告缺失；8/28 周五报告买入日在下周，不计入本周）")
    lines.append("")

    # 每周一批次明细
    for rd in report_days:
        lst = by_day.get(rd, [])
        if not lst:
            lines.append(f"## 📅 {rd} 报告 → {BUY_DATE_MAP.get(rd)} 开盘买入")
            lines.append("")
            lines.append("（无计入本回测的信号）")
            lines.append("")
            continue
        lines.append(f"## 📅 {rd} 报告 → {BUY_DATE_MAP.get(rd)} 开盘买入（{len(lst)} 只）")
        lines.append("")
        hdr = "| 主线 | 股票 | 代码 | 买入开盘 | " + " | ".join(f"{d[5:]}" for d in WEEK_TRADE_DATES[WEEK_TRADE_DATES.index(BUY_DATE_MAP[rd]):WEEK_TRADE_DATES.index(week_end) + 1]) + " | 本周收益 | 结果 |"
        lines.append(hdr)
        lines.append("|:----|:---:|:---:|:---:|" + ":---:|" * len(WEEK_TRADE_DATES[WEEK_TRADE_DATES.index(BUY_DATE_MAP[rd]):WEEK_TRADE_DATES.index(week_end) + 1]) + ":---:|:---:|")
        for r in lst:
            closes = []
            for d in WEEK_TRADE_DATES[WEEK_TRADE_DATES.index(r["buy_date"]):WEEK_TRADE_DATES.index(week_end) + 1]:
                if d in [x["date"] for x in r["daily"]]:
                    closes.append(f"{[x['close'] for x in r['daily'] if x['date'] == d][0]:.2f}")
                else:
                    closes.append("-")
            mark = "✅" if r["success"] else "❌"
            lines.append(f"| {r['theme']} | {r['name']} | {r['code']} | {r['open']:.2f} | " + " | ".join(closes) + f" | **{r['final_ret']:+.2f}%** | {mark} |")
        lines.append("")

    # 本周汇总
    lines.append("## 📊 本周汇总")
    lines.append("")
    lines.append(f"- 有效信号数：**{n}** 只（剔除报告缺失/超出本周/无K线）")
    lines.append(f"- 成功率：**{succ}/{n} = {succ / n * 100:.1f}%**" if n else "- 成功率：N/A")
    lines.append(f"- 平均收益：**{avg_ret:+.2f}%**　中位数：{med_ret:+.2f}%")
    if n:
        best = max(results, key=lambda x: x["final_ret"])
        worst = min(results, key=lambda x: x["final_ret"])
        lines.append(f"- 最佳：{best['name']} {best['final_ret']:+.2f}%　最差：{worst['name']} {worst['final_ret']:+.2f}%")
    lines.append("")

    # 分报告日批次
    lines.append("### 批次统计（按报告日）")
    lines.append("")
    lines.append("| 报告日 | 买入日 | 信号数 | 成功率 | 平均收益 |")
    lines.append("|:---:|:---:|:---:|:---:|:---:|")
    for rd in report_days:
        lst = by_day.get(rd, [])
        if not lst:
            continue
        ns = len(lst)
        ss = sum(1 for r in lst if r["success"])
        avg = sum(r["final_ret"] for r in lst) / ns
        lines.append(f"| {rd} | {BUY_DATE_MAP.get(rd)} | {ns} | {ss}/{ns} ({ss / ns * 100:.0f}%) | {avg:+.2f}% |")
    lines.append("")

    # 逐日组合收益（批次等权）
    lines.append("### 逐日组合收益（各批次自买入日起，等权平均）")
    lines.append("")
    lines.append("| 报告日 | " + " | ".join(d[5:] for d in WEEK_TRADE_DATES) + " |")
    lines.append("|:---:|" + ":---:|" * len(WEEK_TRADE_DATES))
    for rd in report_days:
        if rd not in day_series:
            continue
        row = [f"{rd}"]
        for d in WEEK_TRADE_DATES:
            if d in day_series[rd]:
                vals = day_series[rd][d]
                row.append(f"{sum(vals) / len(vals):+.2f}%")
            else:
                row.append("-")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # 大盘对照
    lines.append("### 大盘对照（上证指数，同期）")
    lines.append("")
    if sh_ref:
        lines.append("| 日期 | 上证收盘 | 较周初涨跌 |")
        lines.append("|:---:|:---:|:---:|")
        base = None
        for d in WEEK_TRADE_DATES:
            if d in sh_ref:
                if base is None:
                    base = sh_ref[d]
                lines.append(f"| {d} | {sh_ref[d]:.2f} | {(sh_ref[d] / base - 1) * 100:+.2f}% |")
    else:
        lines.append("（上证指数K线获取失败）")
    lines.append("")

    lines.append("---")
    lines.append("> ⚠️ 回测说明：① 未计交易成本/滑点；② 未按报告建议仓位加权（等权）；③ 未模拟止损——持有至周末收盘，与报告止损纪律不同；④ 8/28 周五报告买入日 8/31 超出本周，待下周验证。")
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # 控制台摘要
    print("\n=== 回测结果 ===")
    print(f"信号 {n} 只 | 成功率 {succ}/{n} ({succ / n * 100:.1f}%)" if n else "无信号")
    print(f"平均收益 {avg_ret:+.2f}% | 中位数 {med_ret:+.2f}%")
    for rd in report_days:
        lst = by_day.get(rd, [])
        if lst:
            avg = sum(r["final_ret"] for r in lst) / len(lst)
            ss = sum(1 for r in lst if r["success"])
            print(f"  {rd} 批次: {len(lst)}只 成功{ss} 平均{avg:+.2f}%")
    print(f"\n产物: {path}")


if __name__ == "__main__":
    main()