#!/usr/bin/env python3
"""
技术确认共享模块 — MA 破位分级（16:30 fetch_trend / 16:45 fetch_theme_leaders 共用）

单一判据源：16:30/16:45 共用同一套 MA 破位分级，避免两套口径漂移。

判据：
  break_level:
    0 = 未破位（收盘 ≥ MA5）
    1 = 跌破 MA5（但仍 ≥ MA10）
    2 = 跌破 MA5 + MA10（但仍 ≥ MA20）
    3 = 跌破 MA5 + MA10 + MA20（中期趋势破坏）
"""

from typing import List, Optional


def calc_ma(closes: List[float], n: int) -> Optional[float]:
    """最近 N 日简单移动平均（SMA），数据不足返回 None"""
    if not closes or len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def compute_tech_context(closes: List[float], vols: Optional[List[float]] = None) -> Optional[dict]:
    """技术确认维度：均线破位分级 + 近5日跌幅 + 量比 + MA60/MA20斜率。

    Args:
        closes: 收盘价列表（时间升序）
        vols: 成交量列表（时间升序），可选；缺失时量比=1.0
    Returns:
        dict: {close, ma5, ma10, ma20, ma60, ma20_slope_up, break_level, ret5, vol_ratio}
        数据不足（<21根）返回 None（调用方按保守逻辑处理）
        注：ma60/ma20_slope_up 数据不足时为 None，由调用方宽容处理（不误杀）。
    """
    if not closes or len(closes) < 21:
        return None
    close = closes[-1]
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    # MA60 与 MA20 斜率（三维状态机 T 补充判据，设计 3.1 L38）
    ma60 = calc_ma(closes, 60)
    ma20_slope_up = None
    if ma20 is not None and len(closes) >= 25:
        ma20_prev = calc_ma(closes[:-5], 20)
        if ma20_prev is not None:
            ma20_slope_up = ma20 >= ma20_prev  # 向上=走平/抬升；向下=拐头
    # 近5日涨跌幅（%）：5 个交易日前收盘 → 今日收盘
    ret5 = (close / closes[-6] - 1) * 100 if len(closes) >= 6 and closes[-6] else 0.0
    # 量比：当日成交量 / 前5日均量
    vol_ratio = 1.0
    if vols and len(vols) >= 6 and sum(vols[-6:-1]) > 0:
        vol_ratio = vols[-1] / (sum(vols[-6:-1]) / 5)
    break_level = 0
    if ma5 is not None and close < ma5:
        break_level = 1
    if ma10 is not None and close < ma10:
        break_level = 2
    if ma20 is not None and close < ma20:
        break_level = 3
    return {
        "close": close,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "ma20_slope_up": ma20_slope_up,
        "break_level": break_level,
        "ret5": ret5,
        "vol_ratio": vol_ratio,
    }


def break_level_name(level: int) -> str:
    """破位分级可读名称"""
    return {0: "未破位", 1: "破MA5", 2: "破MA5+MA10", 3: "破MA5+MA10+MA20"}.get(level, "未知")


if __name__ == "__main__":
    # 快速自检：20 根单调上涨（未破位）+ 21 根含跌（破位）
    up = [100 + i for i in range(23)]
    tech_up = compute_tech_context(up)
    assert tech_up["break_level"] == 0, tech_up
    dn = [110 - i for i in range(23)]
    tech_dn = compute_tech_context(dn)
    assert tech_dn["break_level"] == 3, tech_dn
    assert compute_tech_context([1, 2, 3]) is None
    print("✅ tech_context 自检通过:", tech_up["break_level"], tech_dn["break_level"])