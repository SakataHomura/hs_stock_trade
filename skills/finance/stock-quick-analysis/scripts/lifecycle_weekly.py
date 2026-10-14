#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周线生命周期数据生成器（stock-quick-analysis 生命周期分析专用）。

数据源：腾讯 kline 接口（ktype=week, qfq 前复权），与 finance_wrapper quotes 同源。
输出：结构化中文摘要（周线MA/MACD/RSI/量能/年度结构），供 Agent 按 SKILL.md 生命周期
      规则判定 T1-T4 / R1-R3 阶段。纯标准库，无第三方依赖。

用法：
    python3 lifecycle_weekly.py --code sh600346
    python3 lifecycle_weekly.py --code sh600346 --weeks 80
输出到 stdout，可重定向落盘（批量模式 /tmp/qa/<code>_weekly.txt）。
"""
import argparse
import json
import sys
import urllib.request
from datetime import date, timedelta


def fetch_weekly(code: str, weeks: int) -> list:
    """拉取周线K线（倒序最新在前），返回 list[dict]。"""
    end = date.today()
    start = end - timedelta(weeks=weeks + 2)  # 多留边界，取整周边界
    url = (
        "https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get"
        f"?code={code}&ktype=week&fqtype=qfq&limit={weeks + 10}"
        f"&fromDate={start.isoformat()}&toDate={end.isoformat()}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0 or not data.get("data"):
        raise RuntimeError(f"周线接口返回异常: {json.dumps(data, ensure_ascii=False)[:200]}")
    nodes = data["data"].get("nodes") or []
    rows = []
    for n in nodes:
        try:
            rows.append({
                "date": n["date"],
                "open": float(n["open"]),
                "close": float(n["last"]),
                "high": float(n["high"]),
                "low": float(n["low"]),
                "vol": float(n["volume"]),  # 股
            })
        except (KeyError, ValueError, TypeError):
            continue
    rows.sort(key=lambda r: r["date"])  # 升序，最新在后
    return rows


def ema_series(values, span):
    """EMA 序列（标准递归定义，等价 pandas ewm adjust=False）。"""
    k = 2.0 / (span + 1)
    out = []
    prev = None
    for v in values:
        if prev is None:
            prev = v
        else:
            prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def macd_weekly(closes):
    """MACD(12,26,9) -> (dif_seq, dea_seq, hist_seq)；hist = 2*(DIF-DEA)。"""
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = ema_series(dif, 9)
    hist = [2 * (d - e) for d, e in zip(dif, dea)]
    return dif, dea, hist


def rsi_weekly(closes, period=14):
    """RSI(14) Wilder 平滑；返回与 closes 等长的 RSI 序列（前 period 个为 None/前缀期）。"""
    if len(closes) < period + 1:
        return [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi = [None] * (period + 1)
    rsi.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return rsi


def last_cross(dif, dea):
    """最近一次 DIF/DEA 交叉：返回 (周数前, 日期, 类型)。金叉=DIF自下而上穿。"""
    for i in range(len(dif) - 1, 0, -1):
        if (dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]):
            return (len(dif) - 1 - i, "金叉")
        if (dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]):
            return (len(dif) - 1 - i, "死叉")
    return (None, None)


def fmt_vol(v):
    """股 -> 万手（1手=100股）。"""
    return f"{v / 100 / 10000:.1f}万手"


def main():
    ap = argparse.ArgumentParser(description="周线生命周期数据")
    ap.add_argument("--code", required=True, help="小写代码，如 sh600346")
    ap.add_argument("--weeks", type=int, default=80, help="拉取周数（默认80≈1.5年，至少覆盖1年）")
    args = ap.parse_args()

    rows = fetch_weekly(args.code, args.weeks)
    if len(rows) < 20:
        print(f"[周线生命周期数据] {args.code} 周线数据不足{len(rows)}周，无法可靠判定生命周期，"
              f"回退日线大结构判断（近3月quotes）。")
        return 0

    closes = [r["close"] for r in rows]
    vols = [r["vol"] for r in rows]
    dates = [r["date"] for r in rows]
    n = len(rows)
    cur_close = closes[-1]
    cur_date = dates[-1]

    # 年度结构：最近52周（不足则全部）
    win = rows[-52:] if n >= 52 else rows
    hi = max(win, key=lambda r: r["high"])
    lo = min(win, key=lambda r: r["low"])
    hi_pct = (cur_close / hi["high"] - 1) * 100
    lo_pct = (cur_close / lo["low"] - 1) * 100
    pos_pct = (cur_close - lo["low"]) / (hi["high"] - lo["low"]) * 100 if hi["high"] > lo["low"] else 50.0

    # 周线均线
    def ma(k):
        return sum(closes[-k:]) / k if n >= k else None
    ma5, ma10, ma20 = ma(5), ma(10), ma(20)
    ma20_prev = sum(closes[-24:-4]) / 20 if n >= 24 else None  # 20周前 MA20 方向
    if ma5 is None or ma10 is None or ma20 is None:
        print(f"[周线生命周期数据] {args.code} 数据不足20周，无法计算均线。")
        return 0
    if ma5 > ma10 > ma20:
        arr = "多头排列(MA5>MA10>MA20)"
    elif ma5 < ma10 < ma20:
        arr = "空头排列(MA5<MA10<MA20)"
    else:
        arr = "缠绕"
    ma20_dir = "向上" if (ma20_prev is not None and ma20 >= ma20_prev) else "向下"

    # MACD
    dif, dea, hist = macd_weekly(closes)
    d, e, h = dif[-1], dea[-1], hist[-1]
    macd_pos = "零轴上方" if d >= 0 else "零轴下方"
    bar = "红柱" if h >= 0 else "绿柱"
    # 最近一次 DIF/DEA 交叉远近距离（周数）
    cross_ago, cross_type = last_cross(dif, dea)
    cross_txt = ""
    if cross_ago is not None:
        if cross_ago == 0:
            cross_txt = f"，本周发生{cross_type}（{dates[-1]}）"
        else:
            cross_txt = f"，最近一次{cross_type}于{cross_ago}周前({dates[n-1-cross_ago]})"
    # 柱体方向变化（近2周）
    if n >= 3:
        if hist[-1] > hist[-2]:
            bar_trend = "柱体放大"
        elif hist[-1] < hist[-2]:
            bar_trend = "柱体缩短"
        else:
            bar_trend = "柱体走平"
    else:
        bar_trend = ""

    # RSI
    rsi = rsi_weekly(closes)
    rsi_cur = rsi[-1]
    rsi_txt = f"{rsi_cur:.1f}" if rsi_cur is not None else "数据不足"
    if rsi_cur is not None:
        if rsi_cur >= 70:
            rsi_txt += "（过热区）"
        elif rsi_cur <= 30:
            rsi_txt += "（弱势区）"
        else:
            rsi_txt += "（中性区）"
    # 52周RSI高点（顶背离辅助：仅在价格仍贴近52周高点时有效；价格已深跌回调则属正常回落）
    rsi_52 = [r for r in rsi[-52:] if r is not None]
    rsi_peak = max(rsi_52) if rsi_52 else None
    # 一年内价格高点对应周 index
    hi_idx = win.index(hi)
    hi_rsi = rsi[n - len(win) + hi_idx] if (n - len(win) + hi_idx) < len(rsi) else None
    diverge_txt = ""
    if abs(hi_pct) <= 10 and rsi_peak is not None and rsi_cur is not None and hi_rsi is not None \
            and rsi_cur < rsi_peak - 5:
        diverge_txt = (f"，注意：价格仍贴近52周高点（距高{abs(hi_pct):.1f}%），"
                       f"而RSI({rsi_cur:.0f})低于高点周RSI({hi_rsi:.0f})，顶背离风险")

    # 量能
    vol_5avg = sum(vols[-6:-1]) / 5 if n >= 6 else None
    vol_ratio = vols[-1] / vol_5avg if vol_5avg else None

    # 输出
    out = []
    out.append(f"[周线生命周期数据] {args.code} 截至{cur_date}（共{n}周，{dates[0]} ~ {cur_date}）")
    out.append("■ 年度结构（近52周）")
    out.append(f"  最高: {hi['high']:.2f}（{hi['date']}周） | 最低: {lo['low']:.2f}（{lo['date']}周） | 现价: {cur_close:.2f}")
    out.append(f"  距高点: {hi_pct:+.1f}% | 距低点: {lo_pct:+.1f}% | 52周区间位置: {pos_pct:.0f}%（0=最低/100=最高）")
    out.append("■ 周线均线")
    out.append(f"  MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f} → {arr}，MA20方向: {ma20_dir}")
    out.append("■ 周线MACD(12,26,9)")
    out.append(f"  DIF={d:+.3f} DEA={e:+.3f} 柱={h:+.3f} → {macd_pos} {bar} {bar_trend}{cross_txt}")
    if diverge_txt:
        out.append(f"  顶背离辅助: {diverge_txt}")
    out.append("■ 周线RSI(14)")
    out.append(f"  RSI={rsi_txt}"
               + (f" | 52周RSI峰值={rsi_peak:.1f}" if rsi_peak is not None else ""))
    out.append("■ 周量能")
    out.append(f"  本周量={fmt_vol(vols[-1])}，5周均量={fmt_vol(vol_5avg) if vol_5avg else 0}，量比={vol_ratio:.2f}" if vol_ratio else "  量能数据不足")
    out.append("■ 近12周K线（升序）")
    out.append("  | 日期 | 开 | 收 | 高 | 低 | 涨跌% | 量(万手) |")
    for idx in range(max(0, n - 12), n):
        r = rows[idx]
        prev_close = closes[idx - 1] if idx > 0 else r["close"]
        chg = (r["close"] / prev_close - 1) * 100
        out.append(f"  | {r['date']} | {r['open']:.2f} | {r['close']:.2f} | {r['high']:.2f} | {r['low']:.2f} | {chg:+.1f} | {r['vol']/100/10000:.1f} |")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())