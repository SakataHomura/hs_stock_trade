#!/usr/bin/env python3
"""
板块汇总脚本
功能：
1. 按照5日涨跌幅查询TOP20的概念板块
2. 查询每个概念板块中的成分股，按照5日涨跌幅排序，提取每个板块TOP10的成分股
3. 将股票进行去重以后，按照5日涨跌幅排序，输出表格1：
   股票名称，股票代码，最新价，当日涨跌幅，5日涨跌幅，20日涨跌幅，所属概念板块
4. 统计每个股票所属的二级行业板块，按照板块出现的次数排序，输出表格2：
   板块名称，板块代码，出现的股票+代码，多个股票逗号分割

数据来源：腾讯财经 API（与 finance_plugin.py 相同接口）
"""

import asyncio
import json
import os
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

import pymysql

# ━━━ HTTP 请求工具 ━━━

async def fetch_url(url: str, timeout: int = 15, max_retries: int = 2) -> str | None:
    """带超时和重试的 URL 请求"""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = urllib.request.urlopen(url, timeout=timeout)
            ret = response.read()
            try:
                return ret.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    return ret.decode('gbk')
                except UnicodeDecodeError:
                    return ret.decode('latin1')
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                await asyncio.sleep(attempt + 1)
    return None


async def fetch_json(url: str) -> dict | None:
    """获取 JSON 响应"""
    res = await fetch_url(url)
    if not res:
        return None
    try:
        return json.loads(res)
    except json.JSONDecodeError:
        return None


# ━━━ API 接口 ━━━

async def fetch_concept_sectors_top20() -> list[dict]:
    """
    获取概念板块排行榜，按当日涨跌幅降序，返回 TOP20。
    板块 API 原生返回 zdf 字段，直接排序即可。
    """
    url = (
        "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?"
        "board_type=gn&sort_type=PriceRatio&direct=down"
        "&selector=not_in_market%3Dgn_fg&count=80"
    )
    print("📥 [1/4] 获取概念板块排行...")
    data = await fetch_json(url)
    if not data or "data" not in data:
        print("  ❌ 未获取到板块排行数据")
        return []

    rank_list = data["data"].get("rank_list", [])
    if not rank_list:
        print("  ❌ 暂无板块排行数据")
        return []

    rank_list.sort(key=lambda x: float(x.get("zdf", 0)), reverse=True)

    top20 = []
    for item in rank_list:
        name = item.get("name", "")
        if "ST" in name or "退" in name:
            continue
        top20.append({
            "name": name,
            "code": item.get("code", ""),
            "zdf": float(item.get("zdf", 0)),
            "zdf_d5": float(item.get("zdf_d5", 0)),
            "zdf_d20": float(item.get("zdf_d20", 0)),
        })
        if len(top20) >= 20:
            break

    print(f"  ✅ TOP20 概念板块")
    for i, s in enumerate(top20, 1):
        print(f"    {i:2d}. {s['name']} | 5日: {s['zdf_d5']:+.2f}% | 20日: {s['zdf_d20']:+.2f}%")

    return top20


async def fetch_sector_constituents(sector_code: str) -> list[dict]:
    """
    获取板块成分股（API 按5日涨幅排序，count=50 覆盖 TOP10 候选）。
    注意：API 仅支持按当日涨幅排序，不返回 zdf_d5/zdf_d20。
    """
    url = (
        f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?"
        f"board_code={sector_code}&sort_type=priceRatio&direct=down"
        f"&_appver=7.3&count=50"
    )
    data = await fetch_json(url)
    if not data or "data" not in data:
        return []

    rank_list = data["data"].get("rank_list", [])
    result = []
    for item in rank_list:
        code = item.get("code", "")
        name = item.get("name", "")
        if "ST" in name or "退" in name:
            continue
        result.append({
            "code": code,
            "name": name,
            "zxj": item.get("zxj", ""),
            "zdf": float(item.get("zdf", 0)),
        })

    return result


async def batch_fetch_klines(stock_codes: list[str], days: int = 40) -> dict[str, list[dict]]:
    """
    批量并发获取个股 K 线数据。
    返回: {stock_code: [kline_node, ...]}
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    kline_data = {}
    semaphore = asyncio.Semaphore(30)  # 高并发
    total = len(stock_codes)
    done_count = 0

    async def fetch_single(code: str):
        nonlocal done_count
        async with semaphore:
            try:
                url = (
                    f"https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get?"
                    f"code={code}&ktype=day&fqtype=qfq&limit=30"
                    f"&fromDate={start_date}&toDate={end_date}"
                )
                res = await fetch_url(url)
                if res:
                    data = json.loads(res)
                    if data.get("code") == 0 and "data" in data and "nodes" in data["data"]:
                        nodes = data["data"]["nodes"]
                        klines = [{"close": float(n["last"])} for n in nodes]
                        done_count += 1
                        if done_count % 50 == 0:
                            print(f"    ... {done_count}/{total}")
                        return code, klines
            except Exception:
                pass
            done_count += 1
            return code, []

    tasks = [fetch_single(code) for code in stock_codes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, tuple):
            code, data = result
            if data:
                kline_data[code] = data

    return kline_data


def calc_5d_change(klines: list[dict]) -> float | None:
    """计算5日涨跌幅"""
    if len(klines) < 6:
        return None
    try:
        return (klines[-1]["close"] / klines[-6]["close"] - 1.0) * 100.0
    except (ValueError, TypeError, IndexError, ZeroDivisionError):
        return None


def calc_20d_change(klines: list[dict]) -> float | None:
    """计算20日涨跌幅"""
    if len(klines) < 21:
        return None
    try:
        return (klines[-1]["close"] / klines[-21]["close"] - 1.0) * 100.0
    except (ValueError, TypeError, IndexError, ZeroDivisionError):
        return None


async def batch_fetch_industry_plates(stock_codes: list[str]) -> dict[str, list[dict]]:
    """
    批量获取个股所属二级行业板块。
    返回: {stock_code: [{"name": 板块名, "id": 板块ID}, ...]}
    """
    semaphore = asyncio.Semaphore(20)
    result_map = {}

    async def fetch_single(code: str):
        async with semaphore:
            url = f"https://ifzq.gtimg.cn/appstock/app/stockinfo/plateNew?code={code}"
            data = await fetch_json(url)
            if not data or "data" not in data:
                return code, []

            plate_data = data["data"]
            plates = []
            if "plate" in plate_data and plate_data["plate"]:
                for plate in plate_data["plate"]:
                    if "name" in plate and "id" in plate:
                        if plate.get("level", "") == "1":
                            continue
                        plates.append({"name": plate["name"], "id": str(plate["id"])})
            return code, plates

    tasks = [fetch_single(code) for code in stock_codes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, tuple):
            code, plates = result
            result_map[code] = plates

    return result_map


# ━━━ 数据库写入 ━━━

def save_to_db(report_date: str, table1_data: list[dict], table2_data: list[dict]):
    """将表格数据写入 MySQL portfolio.sector_summary"""
    conn = pymysql.connect(
        host="127.0.0.1",
        port=3306,
        user="wish",
        password="<DB_PASSWORD>",
        database="portfolio",
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            # 先删除同日期旧数据
            cur.execute("DELETE FROM sector_summary WHERE report_date = %s", (report_date,))

            # 写入表格1：概念板块成分股
            for idx, stock in enumerate(table1_data, 1):
                detail = json.dumps(stock, ensure_ascii=False)
                cur.execute(
                    "INSERT INTO sector_summary (report_date, table_type, rank_no, name, code, detail) "
                    "VALUES (%s, 'concept_stocks', %s, %s, %s, %s)",
                    (report_date, idx, stock["name"], stock["code"], detail),
                )

            # 写入表格2：二级行业板块
            for idx, ind in enumerate(table2_data, 1):
                detail = json.dumps(ind, ensure_ascii=False)
                cur.execute(
                    "INSERT INTO sector_summary (report_date, table_type, rank_no, name, code, detail) "
                    "VALUES (%s, 'industry_plates', %s, %s, %s, %s)",
                    (report_date, idx, ind["name"], ind.get("id", ""), detail),
                )

        print(f"  ✅ 数据已写入 MySQL portfolio.sector_summary（日期: {report_date}）")
    finally:
        conn.close()


# ━━━ 主流程 ━━━

async def main():
    start_time = datetime.now()
    report_date_str = start_time.strftime('%Y-%m-%d')
    print(f"{'='*70}")
    print(f"📊 板块汇总脚本 - {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    # ━━━ Step 1: 获取 TOP20 概念板块 ━━━
    top20_sectors = await fetch_concept_sectors_top20()
    if not top20_sectors:
        print("❌ 未获取到概念板块数据，退出")
        return

    # ━━━ Step 2: 获取各板块成分股（每板块 TOP50 by 当日涨幅）━━━
    print(f"\n📥 [2/4] 获取各板块成分股 TOP50...")
    sector_stocks_raw: dict[str, list[dict]] = {}

    for i, sector in enumerate(top20_sectors, 1):
        print(f"  [{i:2d}/20] {sector['name']}...", end="", flush=True)
        stocks = await fetch_sector_constituents(sector["code"])
        sector_stocks_raw[sector["name"]] = stocks
        print(f" {len(stocks)} 只")
        await asyncio.sleep(0.2)

    # ━━━ Step 3: 批量获取 K 线，计算5日/20日涨跌幅 ━━━
    all_stock_codes = set()
    for stocks in sector_stocks_raw.values():
        for stock in stocks:
            all_stock_codes.add(stock["code"])

    print(f"\n📥 [3/4] 批量获取 K 线数据（{len(all_stock_codes)} 只）...")
    kline_data = await batch_fetch_klines(list(all_stock_codes), days=40)

    # 计算涨跌幅
    stock_change_map: dict[str, dict] = {}
    for code, klines in kline_data.items():
        stock_change_map[code] = {
            "zdf_d5": calc_5d_change(klines),
            "zdf_d20": calc_20d_change(klines),
        }

    filled_5d = sum(1 for v in stock_change_map.values() if v["zdf_d5"] is not None)
    filled_20d = sum(1 for v in stock_change_map.values() if v["zdf_d20"] is not None)
    print(f"  ✅ K线: {len(kline_data)}/{len(all_stock_codes)} | "
          f"5日涨跌幅: {filled_5d} | 20日涨跌幅: {filled_20d}")

    # ━━━ Step 4: 按板块排序取 TOP10，去重 ━━━
    all_stocks: dict[str, dict] = {}

    for sector_name, stocks in sector_stocks_raw.items():
        for stock in stocks:
            code = stock["code"]
            changes = stock_change_map.get(code, {})
            stock["zdf_d5"] = changes.get("zdf_d5")
            stock["zdf_d20"] = changes.get("zdf_d20")

        stocks.sort(
            key=lambda x: x["zdf_d5"] if x["zdf_d5"] is not None else -9999,
            reverse=True
        )

        for stock in stocks[:10]:
            code = stock["code"]
            if code in all_stocks:
                all_stocks[code]["sectors"].append(sector_name)
            else:
                all_stocks[code] = {
                    "name": stock["name"],
                    "code": code,
                    "zxj": stock["zxj"],
                    "zdf": stock["zdf"],
                    "zdf_d5": stock["zdf_d5"],
                    "zdf_d20": stock["zdf_d20"],
                    "sectors": [sector_name],
                }

    print(f"\n  ✅ 各板块 TOP10 去重后 {len(all_stocks)} 只")

    # ━━━ Step 5: 输出表格1 ━━━
    sorted_stocks = sorted(
        all_stocks.values(),
        key=lambda x: x["zdf_d5"] if x["zdf_d5"] is not None else -9999,
        reverse=True
    )

    print(f"\n{'='*100}")
    print(f"📋 表格1：概念板块 TOP10 成分股汇总（按5日涨跌幅排序）")
    print(f"{'='*100}\n")

    print(f"| # | 股票名称 | 股票代码 | 最新价 | 当日涨跌幅 | 5日涨跌幅 | 20日涨跌幅 | 所属概念板块 |")
    print(f"|--:|:-------|:-------|------:|---------:|---------:|----------:|:-----------|")

    for idx, stock in enumerate(sorted_stocks, 1):
        name = stock["name"][:8]
        code = stock["code"]
        zxj = stock["zxj"] if stock["zxj"] else "N/A"
        zdf = f"{stock['zdf']:+.2f}%" if stock["zdf"] is not None else "N/A"
        zdf_d5 = f"{stock['zdf_d5']:+.2f}%" if stock["zdf_d5"] is not None else "N/A"
        zdf_d20 = f"{stock['zdf_d20']:+.2f}%" if stock["zdf_d20"] is not None else "N/A"
        sectors = "、".join(stock["sectors"])
        print(f"| {idx} | {name} | {code} | {zxj} | {zdf} | {zdf_d5} | {zdf_d20} | {sectors} |")

    # ━━━ Step 6: 获取二级行业板块 ━━━
    print(f"\n📥 [4/4] 获取 {len(all_stocks)} 只股票的二级行业板块...")
    stock_industry_map = await batch_fetch_industry_plates(list(all_stocks.keys()))

    filled_industry = sum(1 for v in stock_industry_map.values() if v)
    print(f"  ✅ 行业板块数据: {filled_industry}/{len(all_stocks)} 只")

    # ━━━ Step 7: 统计行业板块，输出表格2 ━━━
    industry_counter: dict[str, dict] = {}

    for code, plates in stock_industry_map.items():
        stock_name = all_stocks[code]["name"]
        for plate in plates:
            plate_id = plate["id"]
            plate_name = plate["name"]
            if plate_id not in industry_counter:
                industry_counter[plate_id] = {
                    "name": plate_name,
                    "id": plate_id,
                    "stocks": [],
                }
            existing_codes = {sc for sc, _ in industry_counter[plate_id]["stocks"]}
            if code not in existing_codes:
                industry_counter[plate_id]["stocks"].append((code, stock_name))

    sorted_industries = sorted(
        industry_counter.values(),
        key=lambda x: len(x["stocks"]),
        reverse=True
    )

    print(f"\n{'='*100}")
    print(f"📋 表格2：二级行业板块统计（按股票出现次数排序）")
    print(f"{'='*100}\n")

    print(f"| # | 板块名称 | 板块代码 | 出现次数 | 股票列表 |")
    print(f"|--:|:-------|:-------|-------:|:-------|")

    for idx, ind in enumerate(sorted_industries, 1):
        name = ind["name"]
        pid = f"pt{ind['id']}"
        count = len(ind["stocks"])
        stocks_str = "、".join(f"{sn}({sc})" for sc, sn in ind["stocks"])
        print(f"| {idx} | {name} | {pid} | {count} | {stocks_str} |")

    # ━━━ 汇总 ━━━
    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    print(f"\n{'='*70}")
    print(f"✅ 完成！耗时 {elapsed:.1f}s")
    print(f"   - 概念板块: {len(top20_sectors)} 个")
    print(f"   - 去重股票: {len(all_stocks)} 只")
    print(f"   - 二级行业: {len(industry_counter)} 个")
    print(f"{'='*70}")

    # ━━━ 保存结果到文件 ━━━
    output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "plate")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"plate_{start_time.strftime('%Y%m%d')}.md")

    # 重新输出到文件
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# 板块汇总报告 - {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        f.write(f"## 表格1：概念板块 TOP10 成分股汇总（按5日涨跌幅排序）\n\n")
        f.write(f"| # | 股票名称 | 股票代码 | 最新价 | 当日涨跌幅 | 5日涨跌幅 | 20日涨跌幅 | 所属概念板块 |\n")
        f.write(f"|--:|:-------|:-------|------:|---------:|---------:|----------:|:-----------|\n")
        for idx, stock in enumerate(sorted_stocks, 1):
            name = stock["name"][:8]
            code = stock["code"]
            zxj = stock["zxj"] if stock["zxj"] else "N/A"
            zdf = f"{stock['zdf']:+.2f}%" if stock["zdf"] is not None else "N/A"
            zdf_d5 = f"{stock['zdf_d5']:+.2f}%" if stock["zdf_d5"] is not None else "N/A"
            zdf_d20 = f"{stock['zdf_d20']:+.2f}%" if stock["zdf_d20"] is not None else "N/A"
            sectors = "、".join(stock["sectors"])
            f.write(f"| {idx} | {name} | {code} | {zxj} | {zdf} | {zdf_d5} | {zdf_d20} | {sectors} |\n")

        f.write(f"\n## 表格2：二级行业板块统计（按股票出现次数排序）\n\n")
        f.write(f"| # | 板块名称 | 板块代码 | 出现次数 | 股票列表 |\n")
        f.write(f"|--:|:-------|:-------|-------:|:-------|\n")
        for idx, ind in enumerate(sorted_industries, 1):
            name = ind["name"]
            pid = f"pt{ind['id']}"
            count = len(ind["stocks"])
            stocks_str = "、".join(f"{sn}({sc})" for sc, sn in ind["stocks"])
            f.write(f"| {idx} | {name} | {pid} | {count} | {stocks_str} |\n")

    print(f"📄 报告已保存: {output_file}")

    # ━━━ 写入数据库 ━━━
    print(f"\n📥 写入数据库...")
    table1_rows = []
    for stock in sorted_stocks:
        table1_rows.append({
            "name": stock["name"],
            "code": stock["code"],
            "zxj": stock["zxj"],
            "zdf": stock["zdf"],
            "zdf_d5": stock["zdf_d5"],
            "zdf_d20": stock["zdf_d20"],
            "sectors": stock["sectors"],
        })

    table2_rows = []
    for ind in sorted_industries:
        table2_rows.append({
            "name": ind["name"],
            "id": f"pt{ind['id']}",
            "count": len(ind["stocks"]),
            "stocks": [{"code": sc, "name": sn} for sc, sn in ind["stocks"]],
        })

    save_to_db(report_date_str, table1_rows, table2_rows)


if __name__ == "__main__":
    asyncio.run(main())
