#!/usr/bin/env python3
"""
板块成分股查询脚本
==================
输入：板块代码或板块名称
从 data/plate/plate_*.md 中提取该板块的个股列表 + 热度趋势 + 龙头分析

用法：
  python3 scripts/sector_stock_list.py pt01801151
  python3 scripts/sector_stock_list.py 化学制药
  python3 scripts/sector_stock_list.py pt01801151 --days 10
"""

import argparse
import glob
import os
import re
import sys
from datetime import datetime

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "plate")


def parse_plate_files():
    """解析所有 plate_*.md 文件，返回 {日期: {板块名: {code, count, stocks}}}"""
    files = sorted(glob.glob(os.path.join(DATA_DIR, "plate_*.md")))
    all_data = {}

    for fpath in files:
        date_str = os.path.basename(fpath).replace("plate_", "").replace(".md", "")
        content = open(fpath, "r", encoding="utf-8").read()

        # 找表格2部分
        table2_match = re.search(r"## 表格2.*?\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
        if not table2_match:
            continue

        table2 = table2_match.group(1)
        plates = {}

        for line in table2.split("\n"):
            line = line.strip()
            if not line.startswith("|") or line.startswith("| #") or line.startswith("|:") or line.startswith("|—"):
                continue

            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 6:
                continue

            name = parts[2]
            code = parts[3]
            count_str = parts[4]
            stock_list_str = parts[5]

            try:
                count = int(count_str)
            except ValueError:
                count = 0

            stocks = []
            if stock_list_str:
                stock_items = re.findall(r'([\u4e00-\u9fff\w\- ]+)\(([sbzsh][hz]\d{6})\)', stock_list_str)
                for sname, scode in stock_items:
                    stocks.append((sname.replace(' ', ''), scode))

            plates[name] = {"code": code, "count": count, "stocks": stocks}

        if plates:
            all_data[date_str] = plates

    return all_data


def find_plate(all_data, query):
    """根据板块代码或名称查找板块"""
    query = query.strip()

    # 先按代码匹配
    for date_str, plates in all_data.items():
        for name, info in plates.items():
            if info["code"] == query:
                return name, info["code"]

    # 再按名称模糊匹配
    for date_str, plates in all_data.items():
        for name in plates:
            if query in name or name in query:
                return name, plates[name]["code"]

    return None, None


def get_plate_history(all_data, plate_name):
    """获取板块的历史热度数据"""
    history = []
    for date_str in sorted(all_data.keys()):
        plates = all_data[date_str]
        if plate_name in plates:
            history.append((date_str, plates[plate_name]["count"], plates[plate_name]["stocks"]))
        else:
            history.append((date_str, 0, []))
    return history


def get_latest_stocks(all_data, plate_name):
    """获取最新一天该板块的股票列表"""
    for date_str in sorted(all_data.keys(), reverse=True):
        plates = all_data[date_str]
        if plate_name in plates:
            return date_str, plates[plate_name]["stocks"]
    return None, []


def trend_judge(history):
    """判断热度趋势"""
    if len(history) < 2:
        return "数据不足"

    recent = history[-5:] if len(history) >= 5 else history
    counts = [h[1] for h in recent]

    if len(counts) >= 3:
        if all(c >= 15 for c in counts[-3:]):
            return "🔥 持续高位"
        if counts[-1] >= 3 * (max(counts[:-1]) or 1):
            return "🟢 暴增启动"
        if counts[-1] < 0.3 * (max(counts[:-1]) or 1) and counts[-2] >= 5:
            return "🔴 骤降预警"
        if counts[-1] > counts[-2] > counts[-3]:
            return "🟢 持续增长"
        if counts[-1] < counts[-2] < counts[-3]:
            return "🔴 持续下降"

    return "⚪ 正常波动"


def format_date(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.strftime("%m/%d")
    except:
        return date_str


def main():
    parser = argparse.ArgumentParser(description="板块成分股查询")
    parser.add_argument("query", help="板块代码或板块名称")
    parser.add_argument("--days", type=int, default=10, help="显示最近N天热度（默认10）")
    args = parser.parse_args()

    all_data = parse_plate_files()
    if not all_data:
        print("❌ 未找到 data/plate/plate_*.md 数据文件")
        return

    plate_name, plate_code = find_plate(all_data, args.query)
    if not plate_name:
        print(f"❌ 未找到板块：{args.query}")
        print("可用板块：")
        seen = set()
        for date_str, plates in all_data.items():
            for name in plates:
                if name not in seen:
                    print(f"  {name} ({plates[name]['code']})")
                    seen.add(name)
        return

    print(f"## 📊 板块成分股查询报告\n")
    print(f"**板块：** {plate_name}（{plate_code}）")
    print(f"**数据范围：** {min(all_data.keys())} ~ {max(all_data.keys())}\n")

    # 热度趋势
    history = get_plate_history(all_data, plate_name)
    trend = trend_judge(history)

    print("---\n### 一、板块热度趋势\n")
    print(f"**趋势判断：** {trend}\n")
    print("| 日期 | 上榜数 | 变化 |")
    print("|:---:|:-----:|:----|")

    recent_history = history[-args.days:] if len(history) > args.days else history
    for i, (date_str, count, _) in enumerate(recent_history):
        if i == 0:
            change = "—"
        else:
            prev = recent_history[i - 1][1]
            diff = count - prev
            if diff > 0:
                change = f"↑ +{diff}"
            elif diff < 0:
                change = f"↓ {diff}"
            else:
                change = "→ 持平"
        print(f"| {format_date(date_str)} | {count} | {change} |")

    # 成分股列表
    latest_date, latest_stocks = get_latest_stocks(all_data, plate_name)
    if latest_stocks:
        print(f"\n---\n### 二、成分股列表（{format_date(latest_date)}，共 {len(latest_stocks)} 只）\n")
        print("| # | 股票 | 代码 |")
        print("|:-:|:---|:----|")
        for i, (sname, scode) in enumerate(latest_stocks, 1):
            print(f"| {i} | {sname} | {scode} |")

    # 龙头个股
    print(f"\n---\n### 三、龙头个股（{format_date(latest_date)}）\n")
    latest_file = os.path.join(DATA_DIR, f"plate_{latest_date}.md")
    if os.path.exists(latest_file):
        content = open(latest_file, "r", encoding="utf-8").read()
        table1_match = re.search(r"## 表格1.*?\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
        if table1_match:
            lines = table1_match.group(1).strip().split("\n")
            data_lines = [l for l in lines if l.startswith("|") and not l.startswith("| #") and not l.startswith("|:") and not l.startswith("|—")]
            sector_stocks = []
            for line in data_lines:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 8:
                    rank = parts[1]
                    name = parts[2]
                    code = parts[3]
                    day_chg = parts[5]
                    week5_chg = parts[6]
                    concepts = parts[7]
                    # 用股票代码交叉匹配
                    if code in {scode for _, scode in latest_stocks}:
                        sector_stocks.append((rank, name, code, day_chg, week5_chg, concepts))

            if sector_stocks:
                # 按5日涨幅排序
                def sort_key(x):
                    try:
                        return float(x[4].replace("%", "").replace("+", ""))
                    except:
                        return 0
                sector_stocks.sort(key=sort_key, reverse=True)
                print("| 排名 | 股票 | 代码 | 当日涨幅 | 5日涨幅 | 概念 |")
                print("|:---:|:---|:----|:-------:|:------:|:----|")
                for rank, name, code, day_chg, week5_chg, concepts in sector_stocks[:10]:
                    print(f"| {rank} | {name} | {code} | {day_chg} | {week5_chg} | {concepts} |")
            else:
                print("（该板块股票未出现在当日概念板块TOP100中）")

    # 热度统计
    print(f"\n---\n### 四、热度变化统计\n")
    print(f"**{plate_name}** 在最近 {min(args.days, len(history))} 天：")
    print(f"- 最高上榜数：{max(h[1] for h in recent_history)} 只")
    print(f"- 最低上榜数：{min(h[1] for h in recent_history)} 只")
    print(f"- 最新上榜数：{recent_history[-1][1]} 只")
    if len(recent_history) >= 3:
        avg = sum(h[1] for h in recent_history[-3:]) / 3
        print(f"- 近3日均值：{avg:.0f} 只")

    print(f"\n---\n*数据来源：data/plate/plate_*.md*")


if __name__ == "__main__":
    main()
