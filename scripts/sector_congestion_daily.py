#!/usr/bin/env python3
"""
板块拥挤度每日统计脚本

功能：
1. 从 MySQL portfolio.sector_summary 读取当天二级行业板块列表
2. 对每个行业板块查询拥挤度数据（成交额占比、平均换手率、涨停/跌停）
3. 将结果保存到本地 data/congestion_YYYYMMDD.md
4. 将结果写入 MySQL portfolio.sector_congestion

数据来源：wequant-finance 的 sector_congestion 接口（直接 HTTP 调用）
"""

import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime

import pymysql

# wequant-finance server.py 中的函数
sys.path.insert(0, "/home/agentuser/.hermes/skills/wequant-finance")
from server import _sector_congestion

# ━━━ 数据库工具 ━━━

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "portfolio",
    "charset": "utf8mb4",
    "autocommit": True,
}


def get_industry_plates_from_db(report_date: str) -> list[dict]:
    """从 sector_summary 表读取当天行业板块列表。"""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, code, detail FROM sector_summary "
                "WHERE report_date = %s AND table_type = 'industry_plates' "
                "ORDER BY rank_no",
                (report_date,),
            )
            rows = cur.fetchall()
            plates = []
            for row in rows:
                detail = json.loads(row[2])
                plates.append({
                    "name": row[0],
                    "code": row[1],
                    "stock_count": detail.get("count", 0),
                })
            return plates
    finally:
        conn.close()


def ensure_congestion_table():
    """确保 sector_congestion 表存在。"""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_congestion (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    report_date DATE NOT NULL,
                    sector_name VARCHAR(100) NOT NULL,
                    sector_code VARCHAR(20) NOT NULL,
                    stock_count INT DEFAULT 0,
                    sector_amount DECIMAL(20,2) DEFAULT 0 COMMENT '板块成交额(万元)',
                    market_amount DECIMAL(20,2) DEFAULT 0 COMMENT '全市场成交额(亿元)',
                    amount_ratio DECIMAL(10,4) DEFAULT 0 COMMENT '成交额占比(%)',
                    avg_turnover DECIMAL(10,4) DEFAULT 0 COMMENT '平均换手率(%)',
                    limitup_count INT DEFAULT 0 COMMENT '涨停家数',
                    limitdown_count INT DEFAULT 0 COMMENT '跌停家数',
                    market_up INT DEFAULT 0 COMMENT '全市场上涨家数',
                    market_down INT DEFAULT 0 COMMENT '全市场下跌家数',
                    market_limitup INT DEFAULT 0 COMMENT '全市场涨停家数',
                    market_limitdown INT DEFAULT 0 COMMENT '全市场跌停家数',
                    congestion_level VARCHAR(20) DEFAULT '' COMMENT '拥挤度等级: 低拥挤/中等拥挤/高拥挤',
                    congestion_signals VARCHAR(200) DEFAULT '' COMMENT '触发信号',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_date_sector (report_date, sector_code)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='板块拥挤度每日数据'
            """)
            print("  ✅ 表 sector_congestion 已就绪")
    finally:
        conn.close()


def save_to_db(report_date: str, records: list[dict]):
    """将拥挤度数据写入 MySQL。"""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            # 先删除同日期旧数据
            cur.execute("DELETE FROM sector_congestion WHERE report_date = %s", (report_date,))

            for r in records:
                cur.execute(
                    """INSERT INTO sector_congestion
                    (report_date, sector_name, sector_code, stock_count,
                     sector_amount, market_amount, amount_ratio, avg_turnover,
                     limitup_count, limitdown_count,
                     market_up, market_down, market_limitup, market_limitdown,
                     congestion_level, congestion_signals)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        report_date,
                        r["name"],
                        r["code"],
                        r["stock_count"],
                        r["sector_amount"],
                        r["market_amount"],
                        r["amount_ratio"],
                        r["avg_turnover"],
                        r["limitup_count"],
                        r["limitdown_count"],
                        r["market_up"],
                        r["market_down"],
                        r["market_limitup"],
                        r["market_limitdown"],
                        r["congestion_level"],
                        r["congestion_signals"],
                    ),
                )
        print(f"  ✅ 已写入 {len(records)} 条记录到 MySQL portfolio.sector_congestion")
    finally:
        conn.close()


# ━━━ 解析 sector_congestion 输出 ━━━

def parse_congestion_output(text: str, sector_name: str, sector_code: str, stock_count: int) -> dict | None:
    """解析 _sector_congestion 返回的文本，提取结构化数据。"""
    try:
        result = {
            "name": sector_name,
            "code": sector_code,
            "stock_count": stock_count,
            "sector_amount": 0,
            "market_amount": 0,
            "amount_ratio": 0,
            "avg_turnover": 0,
            "limitup_count": 0,
            "limitdown_count": 0,
            "market_up": 0,
            "market_down": 0,
            "market_limitup": 0,
            "market_limitdown": 0,
            "congestion_level": "",
            "congestion_signals": "",
        }

        for line in text.split("\n"):
            line = line.strip()

            # 基础数据
            if "板块成交额（全量）：" in line:
                val = line.split("：")[-1].replace("万元", "").replace(",", "").strip()
                result["sector_amount"] = float(val) if val else 0
            elif "全市场成交额：" in line:
                val = line.split("：")[-1].replace("亿元", "").replace(",", "").strip()
                result["market_amount"] = float(val) if val else 0
            elif "成交额占比：" in line:
                val = line.split("：")[-1].replace("%", "").strip()
                result["amount_ratio"] = float(val) if val else 0
            elif "平均换手率（全量）：" in line:
                val = line.split("：")[-1].replace("%", "").strip()
                result["avg_turnover"] = float(val) if val else 0

            # 情绪数据
            elif "涨停家数：" in line and "全市场" not in line:
                val = line.split("：")[-1].strip()
                result["limitup_count"] = int(val) if val else 0
            elif "跌停家数：" in line and "全市场" not in line:
                val = line.split("：")[-1].strip()
                result["limitdown_count"] = int(val) if val else 0
            elif "全市场上涨家数：" in line:
                val = line.split("：")[-1].strip()
                result["market_up"] = int(val) if val else 0
            elif "全市场下跌家数：" in line:
                val = line.split("：")[-1].strip()
                result["market_down"] = int(val) if val else 0
            elif "全市场涨停家数：" in line:
                val = line.split("：")[-1].strip()
                result["market_limitup"] = int(val) if val else 0
            elif "全市场跌停家数：" in line:
                val = line.split("：")[-1].strip()
                result["market_limitdown"] = int(val) if val else 0

            # 拥挤度判断
            elif "当前状态：" in line:
                level = line.split("：")[-1].strip()
                result["congestion_level"] = level

            elif "触发信号：" in line:
                val = line.split("：")[-1].strip()
                result["congestion_signals"] = val

        return result
    except Exception as e:
        print(f"    ⚠️ 解析失败: {e}")
        return None


# ━━━ 主流程 ━━━

async def main():
    start_time = datetime.now()
    report_date = start_time.strftime("%Y-%m-%d")
    report_date_compact = start_time.strftime("%Y%m%d")

    print(f"{'='*70}")
    print(f"📊 板块拥挤度统计 - {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    # ━━━ Step 1: 确保数据库表存在 ━━━
    print("📥 [1/5] 检查数据库表...")
    ensure_congestion_table()

    # ━━━ Step 2: 从 sector_summary 读取行业板块列表 ━━━
    print(f"\n📥 [2/5] 读取 {report_date} 行业板块列表...")
    plates = get_industry_plates_from_db(report_date)
    if not plates:
        print(f"  ⚠️ 未找到 {report_date} 的行业板块数据，尝试读取最近交易日...")
        # 尝试往前找
        conn = pymysql.connect(**DB_CONFIG)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT report_date FROM sector_summary "
                    "WHERE table_type = 'industry_plates' "
                    "ORDER BY report_date DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    report_date = row[0].strftime("%Y-%m-%d")
                    report_date_compact = row[0].strftime("%Y%m%d")
                    print(f"  使用最近交易日: {report_date}")
                    plates = get_industry_plates_from_db(report_date)
        finally:
            conn.close()

    if not plates:
        print("❌ 未找到任何行业板块数据，退出")
        return

    print(f"  ✅ 加载 {len(plates)} 个行业板块")

    # ━━━ Step 3: 逐个查询拥挤度 ━━━
    print(f"\n📥 [3/5] 查询各板块拥挤度...")
    records = []
    total = len(plates)

    for i, plate in enumerate(plates, 1):
        code = plate["code"]
        name = plate["name"]
        stock_count = plate["stock_count"]

        print(f"  [{i:2d}/{total}] {name} ({code})...", end="", flush=True)

        try:
            text = await _sector_congestion(code)
            parsed = parse_congestion_output(text, name, code, stock_count)
            if parsed:
                records.append(parsed)
                level_icon = {"低拥挤 ✅": "🟢", "中等拥挤 ⚠️": "🟡", "高拥挤 ❌": "🔴"}
                icon = level_icon.get(parsed["congestion_level"], "⚪")
                print(f" {icon} {parsed['congestion_level']} | 成交额占比{parsed['amount_ratio']:.1f}% | 换手率{parsed['avg_turnover']:.1f}%")
            else:
                print(" ❌ 解析失败")
        except Exception as e:
            print(f" ❌ 异常: {e}")

        await asyncio.sleep(0.3)  # 避免请求过快

    print(f"\n  ✅ 成功查询 {len(records)}/{total} 个板块")

    # ━━━ Step 4: 写入数据库 ━━━
    print(f"\n📥 [4/5] 写入数据库...")
    save_to_db(report_date, records)

    # ━━━ Step 5: 保存本地文件 ━━━
    print(f"\n📥 [5/5] 保存本地文件...")
    output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"congestion_{report_date_compact}.md")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# 板块拥挤度统计 - {report_date}\n\n")
        market_amount = records[0]['market_amount'] if records else 0
        f.write(f"全市场成交额：{market_amount:.0f}亿元（如有变动以各板块数据为准）\n\n")

        # 按拥挤度等级分组
        high = [r for r in records if "高拥挤" in r["congestion_level"]]
        mid = [r for r in records if "中等拥挤" in r["congestion_level"]]
        low = [r for r in records if "低拥挤" in r["congestion_level"]]

        if high:
            f.write("## 🔴 高拥挤板块\n\n")
            f.write("| 板块名称 | 成交额占比(%) | 平均换手率(%) | 涨停/跌停 | 触发信号 |\n")
            f.write("|----------|-------------|-------------|----------|--------|\n")
            for r in high:
                f.write(f"| {r['name']} | {r['amount_ratio']:.1f} | {r['avg_turnover']:.1f} | {r['limitup_count']}/{r['limitdown_count']} | {r['congestion_signals']} |\n")
            f.write("\n")

        if mid:
            f.write("## 🟡 中等拥挤板块\n\n")
            f.write("| 板块名称 | 成交额占比(%) | 平均换手率(%) | 涨停/跌停 | 触发信号 |\n")
            f.write("|----------|-------------|-------------|----------|--------|\n")
            for r in mid:
                f.write(f"| {r['name']} | {r['amount_ratio']:.1f} | {r['avg_turnover']:.1f} | {r['limitup_count']}/{r['limitdown_count']} | {r['congestion_signals']} |\n")
            f.write("\n")

        if low:
            f.write("## 🟢 低拥挤板块\n\n")
            f.write("| 板块名称 | 成交额占比(%) | 平均换手率(%) | 涨停/跌停 |\n")
            f.write("|----------|-------------|-------------|----------|\n")
            for r in low:
                f.write(f"| {r['name']} | {r['amount_ratio']:.1f} | {r['avg_turnover']:.1f} | {r['limitup_count']}/{r['limitdown_count']} |\n")
            f.write("\n")

        # 全量明细
        f.write("---\n\n")
        f.write("## 全量明细\n\n")
        f.write("| # | 板块名称 | 成交额占比(%) | 平均换手率(%) | 涨停/跌停 | 拥挤度 | 触发信号 |\n")
        f.write("|---|---------|-------------|-------------|----------|-------|--------|\n")
        for i, r in enumerate(records, 1):
            level_icon = {"低拥挤 ✅": "🟢", "中等拥挤 ⚠️": "🟡", "高拥挤 ❌": "🔴"}
            icon = level_icon.get(r["congestion_level"], "⚪")
            f.write(f"| {i} | {r['name']} | {r['amount_ratio']:.1f} | {r['avg_turnover']:.1f} | {r['limitup_count']}/{r['limitdown_count']} | {icon} {r['congestion_level']} | {r['congestion_signals']} |\n")

    print(f"  ✅ 报告已保存: {output_file}")

    # ━━━ 汇总 ━━━
    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    print(f"\n{'='*70}")
    print(f"✅ 完成！耗时 {elapsed:.1f}s")
    print(f"   - 行业板块: {len(records)} 个")
    print(f"   - 高拥挤: {len(high)} | 中等拥挤: {len(mid)} | 低拥挤: {len(low)}")
    print(f"   - 数据库: portfolio.sector_congestion")
    print(f"   - 本地文件: {output_file}")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
