#!/usr/bin/env python3
"""
板块成分股初筛评分脚本
======================
输入：板块代码（如 pt01801034）
流程：
  1. 获取板块成分股列表（涨幅排序）
  2. 对每只成分股执行 stock_item_features 评分（7维度）
  3. 汇总输出评分排名表

用法：
  python3 scripts/sector_stock_screening.py pt01801034
  python3 scripts/sector_stock_screening.py pt01801034 --top 20
  python3 scripts/sector_stock_screening.py pt01801034 --top 30 --verbose
"""

import asyncio
import argparse
import json
import logging
import re
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stock_item_features_standalone import (
    stock_item_features,
    getUrl,
)

logger = logging.getLogger("sector_screening")


def parse_score_from_md(md_text: str) -> dict:
    """从评分 markdown 中解析各维度分数和总分。"""
    result = {
        "total_score": 0,
        "momentum": 0,
        "volume": 0,
        "structure": 0,
        "resilience": 0,
        "ma20_support": 0,
        "funding": 0,
        "health": 0,
        "pe": None,
        "pe_abnormal": False,
        "warnings": [],
    }

    # 解析各维度分数（格式：动量衰减率：20/20）
    dims = {
        "动量衰减率": "momentum",
        "量能压缩比": "volume",
        "结构紧凑度": "structure",
        "相对板块抗跌性": "resilience",
        "中期均线支撑": "ma20_support",
        "短期资金蓄力": "funding",
        "量价结构健康度": "health",
    }
    for cn_name, en_name in dims.items():
        m = re.search(rf'{cn_name}[：:]*\s*(\d+)/(\d+)', md_text)
        if m:
            result[en_name] = int(m.group(1))

    # 计算总分（各维度分数之和）
    total = 0
    for dim_key in ["momentum", "volume", "structure", "resilience", "ma20_support", "funding", "health"]:
        total += result.get(dim_key, 0)
    result["total_score"] = total

    # 解析 PE
    m = re.search(r'PE[=：:]*\s*([-\d.]+)', md_text)
    if m:
        result["pe"] = float(m.group(1))

    # PE 异常标记
    if "PE极端异常" in md_text:
        # 检查异常判断结果
        pe_section = md_text.split("PE极端异常")[1][:100] if "PE极端异常" in md_text else ""
        if "通过" not in pe_section and "不触发" not in pe_section:
            result["pe_abnormal"] = True

    return result


async def get_sector_constituents(board_code: str, top_n: int = 30) -> list:
    """获取板块成分股列表，按涨幅降序排列。返回 [(code, name, change_pct), ...]"""
    url = f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code={board_code}&sort_type=priceRatio&direct=down&_appver=7.3&count={top_n}"
    logger.info(f"Fetching constituents: {url}")
    raw = await getUrl(url)
    data = json.loads(raw)

    stocks = []
    if data.get("code") == 0 and "data" in data:
        rank_list = data["data"].get("rank_list", data["data"])
        if isinstance(rank_list, dict):
            rank_list = []
        for item in rank_list:
            code = item.get("code", "")
            name = item.get("name", "")
            change_pct = float(item.get("zdf", 0))
            if code:
                stocks.append((code, name, change_pct))
    return stocks


async def get_sector_name(board_code: str) -> str:
    """获取板块名称。"""
    url = f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy2&sort_type=PriceRatioD20&direct=down&count=200"
    try:
        raw = await getUrl(url)
        data = json.loads(raw)
        if data.get("code") == 0 and "data" in data:
            for item in data["data"]:
                if item.get("code") == board_code:
                    return item.get("name", board_code)
    except:
        pass
    return board_code


def score_grade(total: int) -> str:
    if total >= 60:
        return "⭐ 优质"
    elif total >= 45:
        return "✅ 良好"
    elif total >= 30:
        return "⚠️ 一般"
    else:
        return "❌ 弱势"


async def screen_sector(board_code: str, top_n: int = 126, verbose: bool = False):
    """板块成分股初筛评分主流程。"""
    start_time = time.time()

    # 1. 获取板块名称
    sector_name = await get_sector_name(board_code)
    print(f"## 📊 板块成分股初筛评分报告\n")
    print(f"**板块：** {sector_name}（{board_code}）")
    print(f"**评分范围：** 涨幅前 {top_n} 只成分股")
    print(f"**评分体系：** 7维度（动量衰减/量能压缩/结构紧凑/抗跌性/均线支撑/资金蓄力/量价健康），满分70分\n")

    # 2. 获取成分股列表
    stocks = await get_sector_constituents(board_code, top_n)
    if not stocks:
        print("❌ 未获取到成分股数据")
        return

    print(f"**成分股数量：** {len(stocks)} 只\n")

    # 3. 并发评分
    sem = asyncio.Semaphore(5)

    async def score_one(code: str, name: str, change_pct: float):
        async with sem:
            try:
                if verbose:
                    logger.info(f"Scoring {code} {name}...")
                md = await stock_item_features(code=code)
                parsed = parse_score_from_md(md)
                parsed["code"] = code
                parsed["name"] = name
                parsed["change_pct"] = change_pct
                parsed["md"] = md
                return parsed
            except Exception as e:
                logger.error(f"Failed to score {code} {name}: {e}")
                return {
                    "code": code, "name": name, "change_pct": change_pct,
                    "total_score": 0, "momentum": 0, "volume": 0,
                    "structure": 0, "resilience": 0, "ma20_support": 0,
                    "funding": 0, "health": 0, "pe": None, "pe_abnormal": False,
                    "warnings": [str(e)], "md": f"❌ 评分失败：{e}",
                }

    tasks = [score_one(code, name, chg) for code, name, chg in stocks]
    results = await asyncio.gather(*tasks)

    # 4. 过滤流通市值
    min_cap = getattr(args, 'min_cap', 0) if 'args' in dir() else 0
    if min_cap > 0:
        before = len(results)
        results = [r for r in results if r.get('market_cap_yi') is not None and r['market_cap_yi'] >= min_cap]
        filtered = before - len(results)
        if filtered > 0:
            print(f"**流通市值过滤：** 剔除 {filtered} 只市值 < {min_cap}亿 的股票\n")

    # 5. 按总分排序
    results.sort(key=lambda r: r["total_score"], reverse=True)

    # 6. 输出汇总表
    print("---\n### 评分汇总排名\n")
    print("| 排名 | 股票 | 代码 | 涨幅 | 总分 | 动量 | 量能 | 结构 | 抗跌 | 均线 | 资金 | 健康 | PE | 市值(亿) | 评级 |")
    print("|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")

    for i, r in enumerate(results, 1):
        pe_str = f"{r['pe']:.0f}" if r['pe'] is not None else "N/A"
        if r['pe_abnormal']:
            pe_str += "⚠️"
        print(f"| {i} | {r['name']} | {r['code']} | {r['change_pct']:+.2f}% | **{r['total_score']}** | {r['momentum']} | {r['volume']} | {r['structure']} | {r['resilience']} | {r['ma20_support']} | {r['funding']} | {r['health']} | {pe_str} | {score_grade(r['total_score'])} |")

    # 7. 优质标的详情
    top_results = [r for r in results if r['total_score'] >= 45]
    if top_results:
        print("\n---\n### ⭐ 优质/良好标的详情\n")
        for r in top_results:
            print(f"#### {r['name']}（{r['code']}）— 总分 {r['total_score']}/70\n")
            score_section = r['md'].split('[短期蓄力质量度评分明细]')
            if len(score_section) > 1:
                detail = score_section[1].strip()
                print(detail)
            print()

    # 7. 综合推荐
    print("---\n### 综合推荐\n")
    top3 = results[:3]
    print("| 优先级 | 股票 | 代码 | 涨幅 | 总分 | 评级 |")
    print("|:---:|:---:|:---:|:---:|:---:|:---:|")
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(top3):
        print(f"| {medals[i]} | {r['name']} | {r['code']} | {r['change_pct']:+.2f}% | **{r['total_score']}** | {score_grade(r['total_score'])} |")

    elapsed = time.time() - start_time
    print(f"\n---\n*评分完成，耗时 {elapsed:.1f}s*")


def main():
    parser = argparse.ArgumentParser(
        description="板块成分股初筛评分",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 scripts/sector_stock_screening.py pt01801034
  python3 scripts/sector_stock_screening.py pt01801034 --top 20
  python3 scripts/sector_stock_screening.py pt01801034 --top 30 --verbose
        """,
    )
    parser.add_argument("board_code", help="板块代码（如 pt01801034）")
    parser.add_argument("--top", type=int, default=126, help="取涨幅前N只成分股（默认126）")
    parser.add_argument("-v", "--verbose", action="store_true", help="开启 DEBUG 日志")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    asyncio.run(screen_sector(args.board_code, args.top, args.verbose))


if __name__ == "__main__":
    main()
