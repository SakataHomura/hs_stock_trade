#!/usr/bin/env python3
"""
补充获取板块大市值龙头（弥补 finance_plugin constituent 只输出前30只的截断问题）。
2026-08-21 升级：增加均线结构验证（均线照妖镜），剔除弱势股/超跌反弹，标注入场机会。

用法：
    python3 fetch_sector_leaders.py <板块代码> [--top-n 10]

功能：
    1. 直接调用腾讯接口获取板块全部成分股
    2. 按流通市值排序，找出大市值龙头
    3. 结合 finance_plugin 的 RPS 数据交叉验证
    4. 均线结构验证：多头排列+年线翻多=真龙头；年线压制/跌破MA20=剔除
    5. 入场机会筛查：真龙头且回踩到位/乖离适中 → 🟢优选；赶顶 → 🟠勿追
    6. 输出被截断的大市值龙头列表
"""

import asyncio
import json
import sys
import urllib.request
from typing import List, Tuple, Optional, Dict


async def get_url(url: str, timeout: int = 10) -> str:
    """异步获取URL内容"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: urllib.request.urlopen(url, timeout=timeout).read().decode())


async def get_all_constituents(board_code: str) -> List[dict]:
    """获取板块全部成分股（腾讯接口，按当日涨幅排序）"""
    url = f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code={board_code}&sort_type=priceRatio&direct=down&count=200"
    raw = await get_url(url)
    data = json.loads(raw)
    if data.get("code") != 0 or "data" not in data:
        return []
    return data["data"].get("rank_list", [])


async def get_stock_kline(code: str, limit: int = 25) -> Optional[List[float]]:
    """获取个股K线数据，返回收盘价列表"""
    url = f"https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get?code={code}&ktype=day&fqtype=qfq&limit={limit}&fromDate=&toDate="
    try:
        raw = await get_url(url)
        data = json.loads(raw)
        if data.get("code") == 0 and "data" in data and "nodes" in data["data"]:
            nodes = data["data"]["nodes"]
            return [float(n["last"]) for n in nodes]
    except:
        pass
    return None


def calc_rps(close_prices: List[float], all_d20_returns: List[Tuple[str, float]]) -> Tuple[Optional[float], Optional[float]]:
    """计算个股的20日涨幅和RPS"""
    if len(close_prices) < 20:
        return None, None
    d20 = (close_prices[-1] - close_prices[-20]) / close_prices[-20] * 100
    # 计算RPS
    total = len(all_d20_returns)
    rank = sum(1 for _, r in all_d20_returns if r < d20)
    rps = (rank / total) * 100 if total > 0 else None
    return d20, rps


def calc_ma(prices: List[float], n: int) -> Optional[float]:
    """计算MA_n，数据不足返回None"""
    if len(prices) < n:
        return None
    return sum(prices[-n:]) / n


def classify_ma(close: float, ma5, ma10, ma20, ma60, ma120, ma250, bias6, rps=None) -> Tuple[str, str]:
    """
    均线照妖镜（sector-leader-analysis 工具A/B）：
    返回 (结构分类, 入场判定)
    结构分类: 真龙头 / 活跃股 / 超跌反弹 / 弱势
    入场判定: 🟢优选 / 🟡回踩观察 / 🟠乖离过大勿追 / 🟡观察 / ⚪一般 / 🔴剔除
    """
    if any(x is None for x in (ma5, ma10, ma20, ma60)):
        return "数据不足", "⚪ 数据不足"
    if ma20 is None or ma20 <= 0:
        return "数据不足", "⚪ 数据不足"

    # 多头排列 + 年线翻多 = 真·慢牛
    bullish = close > ma5 > ma10 > ma20 > ma60 and (ma250 is None or close > ma250)
    dev_ma20 = (close / ma20 - 1) * 100

    if bullish:
        if bias6 is not None and bias6 > 15:
            return "真龙头", "🟠 乖离过大勿追（BIAS6>15%，等深度回踩）"
        if dev_ma20 <= 8:
            return "真龙头", "🟢 优选（趋势完好，回踩可介入）"
        if dev_ma20 <= 15:
            return "真龙头", "🟡 回踩观察（偏离MA20 8~15%，等回踩MA10/MA20）"
        return "真龙头", "🟠 乖离过大勿追（偏离MA20>15%，等深度回踩）"

    # 站上MA20但长期均线未走好 = 活跃/反弹
    if close > ma20:
        if ma250 is not None and close < ma250:
            return "超跌反弹", "🔴 剔除（年线压制，反弹非趋势）"
        # 活跃股：长期均线未走强，仅当 RPS≥50 才值得观察
        if rps is not None and rps >= 50:
            return "活跃股", "🟡 观察（站上MA20，RPS≥50，等MA60走强）"
        return "活跃股", "⚪ 一般（站上MA20但强度不足）"

    # 收盘 < MA20 = 弱势/破位
    return "弱势", "🔴 剔除（跌破MA20，趋势走弱）"


async def main():
    if len(sys.argv) < 2:
        print("用法: python3 fetch_sector_leaders.py <板块代码> [--top-n 10]")
        sys.exit(1)

    board_code = sys.argv[1]
    top_n = 10
    if "--top-n" in sys.argv:
        idx = sys.argv.index("--top-n")
        if idx + 1 < len(sys.argv):
            top_n = int(sys.argv[idx + 1])

    # 1. 获取全部成分股
    stocks = await get_all_constituents(board_code)
    if not stocks:
        print(f"❌ 未获取到板块 {board_code} 的成分股数据")
        return

    print(f"## 📊 板块大市值龙头补充查询\n")
    print(f"**板块代码：** {board_code}")
    print(f"**成分股总数：** {len(stocks)} 只")
    print(f"**数据来源：** 腾讯财经接口（getBoardRankList）")
    print(f"**排序方式：** 默认按当日涨幅降序\n")

    # 2. 提取所有成分股信息
    stock_info = []
    for s in stocks:
        code = s.get("code", "")
        name = s.get("name", "")
        ltsz = float(s.get("ltsz", 0))  # 流通市值（亿）
        zdf = float(s.get("zdf", 0))    # 今日涨幅
        zxj = float(s.get("zxj", 0))    # 最新价
        turnover = float(s.get("turnover", 0))  # 成交额（万）
        hsl = float(s.get("hsl", 0))    # 换手率
        pe = s.get("pe_ttm", "N/A")
        if code and name:
            stock_info.append({
                "code": code,
                "name": name,
                "ltsz": ltsz,
                "zdf": zdf,
                "zxj": zxj,
                "turnover": turnover,
                "hsl": hsl,
                "pe": pe,
            })

    # 3. 按流通市值排序
    stock_info.sort(key=lambda x: x["ltsz"], reverse=True)

    # 4. 获取前N只大市值股票的K线数据，计算20日涨幅和RPS
    print(f"### 大市值龙头（按流通市值排序，前{top_n}只）\n")
    print(f"| 排名 | 股票名称 | 代码 | 流通市值(亿) | 今日涨幅 | 最新价 | 成交额(万) | 换手率 | PE_TTM |")
    print(f"|:---:|:--------|:---:|:----------:|:-------:|:-----:|:--------:|:-----:|:-----:|")

    for i, s in enumerate(stock_info[:top_n], 1):
        print(f"| {i} | {s['name']} | {s['code']} | {s['ltsz']:.2f} | {s['zdf']:+.2f}% | {s['zxj']:.2f} | {s['turnover']:.0f} | {s['hsl']:.2f}% | {s['pe']} |")

    # 5. 计算所有成分股的20日涨幅（用于RPS排名）
    print(f"\n### 大市值龙头RPS分析\n")
    print(f"正在获取全部成分股的K线数据计算RPS...\n")

    all_d20_returns = []
    kline_cache: Dict[str, List[float]] = {}
    for i, s in enumerate(stock_info):
        close_prices = await get_stock_kline(s["code"])
        if close_prices:
            kline_cache[s["code"]] = close_prices
            if len(close_prices) >= 20:
                d20 = (close_prices[-1] - close_prices[-20]) / close_prices[-20] * 100
                all_d20_returns.append((s["code"], d20))
        if (i + 1) % 20 == 0:
            print(f"  已处理 {i+1}/{len(stock_info)}...")

    print(f"  共获取 {len(all_d20_returns)}/{len(stock_info)} 只股票的20日涨幅\n")

    # 6. 计算前N只大市值股票的RPS + 均线结构验证 + 入场筛查
    print(f"| 股票名称 | 流通市值(亿) | 20日涨幅 | RPS_20 | MA20偏离度 | 今日涨幅 | 均线结构 | 入场判定 |")
    print(f"|:--------|:----------:|:-------:|:-----:|:---------:|:-------:|:----|:----|")

    ma_results = []  # (code, name, ltsz, rps, d20, zdf, structure, entry, dev_ma20, bias6)
    for s in stock_info[:top_n]:
        close_prices = kline_cache.get(s["code"]) or await get_stock_kline(s["code"], limit=260)
        if close_prices and len(close_prices) >= 20:
            d20, rps = calc_rps(close_prices, all_d20_returns)
            ma20 = calc_ma(close_prices, 20)
            ma20_dev = (close_prices[-1] / ma20 - 1) * 100 if ma20 else None

            # 均线结构（需要较长K线，缓存若不足260根则重拉）
            if len(close_prices) < 260:
                close_prices = await get_stock_kline(s["code"], limit=260) or close_prices
                kline_cache[s["code"]] = close_prices

            close = close_prices[-1]
            ma5 = calc_ma(close_prices, 5)
            ma10 = calc_ma(close_prices, 10)
            ma60 = calc_ma(close_prices, 60)
            ma120 = calc_ma(close_prices, 120)
            ma250 = calc_ma(close_prices, 250)
            bias6 = (close / ma5 - 1) * 100 if ma5 else None

            structure, entry = classify_ma(close, ma5, ma10, ma20, ma60, ma120, ma250, bias6, rps)

            # 兼容旧判据：RPS <20 且非真龙头 → 弱势
            if structure == "弱势" and rps is not None and rps > 50 and s["ltsz"] > 300:
                structure = "活跃股"
                entry = "🟡 观察（RPS强但均线未多头）"

            ma_results.append((s["code"], s["name"], s["ltsz"], rps, d20, s["zdf"], structure, entry,
                               f"{ma20_dev:+.2f}%" if ma20_dev is not None else "N/A",
                               f"{bias6:+.2f}%" if bias6 is not None else "N/A"))

            print(f"| {s['name']} | {s['ltsz']:.2f} | {d20:+.2f}% | {rps:.2f} | {ma20_dev:+.2f}% | {s['zdf']:+.2f}% | {structure} | {entry} |")
        else:
            structure, entry = "数据不足", "⚪ 数据不足"
            ma_results.append((s["code"], s["name"], s["ltsz"], None, None, s["zdf"], structure, entry, "N/A", "N/A"))
            print(f"| {s['name']} | {s['ltsz']:.2f} | N/A | N/A | N/A | {s['zdf']:+.2f}% | {structure} | {entry} |")

    # 6.5 入场机会聚焦（重点标注：🟢优选 + 🟡回踩观察 + 🟡观察）
    entry_focus = [r for r in ma_results if any(t in r[7] for t in ("🟢 优选", "🟡 回踩", "🟡 观察"))]
    if entry_focus:
        print(f"\n### 🎯 入场机会聚焦（重点关注）\n")
        print(f"| 股票名称 | 代码 | 流通市值(亿) | RPS_20 | MA20偏离 | BIAS_6 | 均线结构 | 入场判定 |")
        print(f"|:--------|:---:|:----------:|:-----:|:------:|:-----:|:----|:----|")
        for code, name, ltsz, rps, d20, zdf, structure, entry, dev_ma20, bias6 in entry_focus:
            rps_s = f"{rps:.2f}" if rps is not None else "N/A"
            print(f"| {name} | {code} | {ltsz:.2f} | {rps_s} | {dev_ma20} | {bias6} | {structure} | {entry} |")
        print(f"\n> 筛选口径：均线结构（多头排列+年线翻多=真龙头）+ 位置（乖离适中/回踩到位）+ 剔除超跌反弹/弱势/赶顶。")

    # 6.6 乖离警示（真龙头但偏离过大，等深度回踩）
    overextend = [r for r in ma_results if "🟠 乖离过大" in r[7]]
    if overextend:
        print(f"\n### ⚠️ 乖离警示（真龙头/强势但偏离过大，等深度回踩）\n")
        print(f"| 股票名称 | 流通市值(亿) | RPS_20 | 今日涨幅 | MA20偏离 | BIAS_6 | 入场判定 |")
        print(f"|:--------|:----------:|:-----:|:-------:|:------:|:-----:|:----|")
        for code, name, ltsz, rps, d20, zdf, structure, entry, dev_ma20, bias6 in overextend:
            rps_s = f"{rps:.2f}" if rps is not None else "N/A"
            print(f"| {name} | {ltsz:.2f} | {rps_s} | {zdf:+.2f}% | {dev_ma20} | {bias6} | {entry} |")

    # 6.7 剔除清单（超跌反弹/弱势）
    dropped_list = [r for r in ma_results if "🔴 剔除" in r[7]]
    if dropped_list:
        print(f"\n### 🚫 已剔除（超跌反弹/弱势）\n")
        print(f"| 股票名称 | 流通市值(亿) | RPS_20 | 今日涨幅 | 均线结构 | 剔除原因 |")
        print(f"|:--------|:----------:|:-----:|:-------:|:----|:----|")
        for code, name, ltsz, rps, d20, zdf, structure, entry, dev_ma20, bias6 in dropped_list:
            rps_s = f"{rps:.2f}" if rps is not None else "N/A"
            print(f"| {name} | {ltsz:.2f} | {rps_s} | {zdf:+.2f}% | {structure} | {entry.replace('🔴 剔除（','').rstrip('）')} |")

    # 7. 对比：被截断的大市值龙头
    print(f"\n### 被截断的龙头（不在finance_plugin前30只中）\n")

    # 获取finance_plugin的前30只（按当日涨幅排序）
    constituent_url = f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code={board_code}&sort_type=priceRatio&direct=down&_appver=7.3&count=30"
    raw = await get_url(constituent_url)
    data = json.loads(raw)
    top30_codes = set()
    if data.get("code") == 0 and "data" in data:
        for item in data["data"].get("rank_list", []):
            c = item.get("code", "")
            if c:
                top30_codes.add(c)

    # 找出市值大但不在前30的
    missing = [s for s in stock_info if s["code"] not in top30_codes and s["ltsz"] > 200]
    if missing:
        print(f"以下大市值龙头（流通市值>200亿）不在finance_plugin前30只成分股中：\n")
        print(f"| 股票名称 | 流通市值(亿) | 今日涨幅 | 当日排名 | 说明 |")
        print(f"|:--------|:----------:|:-------:|:-------:|:----|")
        for i, s in enumerate(stock_info):
            if s["code"] not in top30_codes and s["ltsz"] > 200:
                rank = next((j+1 for j, ss in enumerate(stock_info) if ss["code"] == s["code"]), "?")
                print(f"| {s['name']} | {s['ltsz']:.2f} | {s['zdf']:+.2f}% | 第{rank}名 | 今日涨幅不大，被截断 |")
    else:
        print("✅ 所有大市值龙头均在前30只成分股中\n")

    # 8. 总结
    print(f"\n### 总结\n")
    print(f"- 板块共 {len(stocks)} 只成分股，finance_plugin 仅输出前30只（按当日涨幅排序）")
    print(f"- 按流通市值排序，前{top_n}只大市值龙头已列出，并完成均线结构验证")
    print(f"- 🟢优选/🟡观察标的见「🎯 入场机会聚焦」；🔴剔除标的见「🚫 已剔除」")
    print(f"- 判定口径：多头排列(价>MA5>MA10>MA20>MA60)+年线翻多=真龙头；跌破MA20/年线压制=超跌反弹/弱势")


if __name__ == "__main__":
    asyncio.run(main())