#!/usr/bin/env python3
"""
板块追踪系统 - MySQL版
数据库：portfolio（MySQL 127.0.0.1）
表：sector_tracker, sector_tracker_checkins, sector_tracker_leaders
"""

import pymysql
import sys
import os
from datetime import datetime

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "portfolio",
    "charset": "utf8mb4",
    "autocommit": True,
}

def get_conn():
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB_CONFIG)

def cmd_add(args):
    """添加板块到追踪"""
    if len(args) < 5:
        print("用法: add <sector_code> <sector_name> <ratio> <rank> <reason> [source] [stage_detail]")
        return
    code, name, ratio, rank, reason = args[0], args[1], float(args[2]), int(args[3]), args[4]
    source = args[5] if len(args) > 5 else 'tracking'
    stage_detail = args[6] if len(args) > 6 else None
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if source == 'trend':
                cur.execute(
                    "INSERT INTO sector_tracker (sector_code, sector_name, added_date, added_ratio, added_rank, added_reason, source, recommended_date, recommended_reason, stage_detail) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (code, name, today, ratio, rank, reason, source, today, reason, stage_detail)
                )
            else:
                cur.execute(
                    "INSERT INTO sector_tracker (sector_code, sector_name, added_date, added_ratio, added_rank, added_reason, source, stage_detail) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (code, name, today, ratio, rank, reason, source, stage_detail)
                )
        print(f"✅ 已添加 {name}({code}) 到追踪列表")
        print(f"   日期: {today} | 涨股比: {ratio}% | 排名: {rank}/124")
        print(f"   来源: {source} | 阶段: {stage_detail or 'N/A'}")
        print(f"   原因: {reason}")
    except pymysql.IntegrityError:
        print(f"⚠️ {name}({code}) 已在追踪列表中")
    finally:
        conn.close()

def cmd_checkin(args):
    """记录每日检查"""
    if len(args) < 9:
        print("用法: checkin <sector_code> <ratio> <rank> <d5> <d20> <trend> <stage> <flow5> <list_count> [signal_type] [note] [source]")
        return
    code = args[0]
    ratio = float(args[1]) if args[1] != 'None' else None
    rank = int(args[2]) if args[2] != 'None' else None
    d5 = float(args[3]) if args[3] != 'None' else None
    d20 = float(args[4]) if args[4] != 'None' else None
    trend = args[5] if args[5] != 'None' else None
    stage = args[6] if args[6] != 'None' else None
    flow5 = float(args[7]) if args[7] != 'None' else None
    list_count = int(args[8]) if args[8] != 'None' else None
    signal_type = args[9] if len(args) > 9 else None
    note = args[10] if len(args) > 10 else None
    source = args[11] if len(args) > 11 else 'tracking'
    today = datetime.now().strftime("%Y-%m-%d")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sector_tracker_checkins (sector_code, check_date, ratio, rank_no, d5, d20, trend, stage, flow5, list_count, signal_type, note, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (code, today, ratio, rank, d5, d20, trend, stage, flow5, list_count, signal_type, note, source)
            )
        print(f"✅ 已记录 {code} 的 {today} 检查数据")
    finally:
        conn.close()

def cmd_list(args):
    """查看追踪列表"""
    source_filter = None
    if args and args[0] in ('tracking', 'trend', 'all'):
        source_filter = args[0]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if source_filter and source_filter != 'all':
                cur.execute("""
                    SELECT s.*,
                        (SELECT check_date FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_check,
                        (SELECT ratio FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_ratio,
                        (SELECT list_count FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_list_count,
                        (SELECT signal_type FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_signal
                    FROM sector_tracker s
                    WHERE s.source = %s
                    ORDER BY FIELD(s.status, 'tracking', 'confirmed', 'failed', 'removed'), s.added_date DESC
                """, (source_filter,))
            else:
                cur.execute("""
                    SELECT s.*,
                        (SELECT check_date FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_check,
                        (SELECT ratio FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_ratio,
                        (SELECT list_count FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_list_count,
                        (SELECT signal_type FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_signal
                    FROM sector_tracker s
                    ORDER BY FIELD(s.source, 'tracking', 'trend'), FIELD(s.status, 'tracking', 'confirmed', 'failed', 'removed'), s.added_date DESC
                """)
            rows = cur.fetchall()

            if not rows:
                print("📭 追踪列表为空")
                return

            print(f"📋 板块追踪列表（共 {len(rows)} 条）\n")

            for r in rows:
                status_icon = {"tracking": "🟡", "confirmed": "🟢", "failed": "🔴", "removed": "⚪"}
                icon = status_icon.get(r['status'], "⚪")
                push_tag = " 📨已推送" if r['push_sent'] else ""
                source_tag = "📌趋势推荐" if r['source'] == 'trend' else "🔍待启动追踪"
                stage_tag = f" | 阶段: {r['stage_detail']}" if r['stage_detail'] else ""
                print(f"{icon} {r['sector_name']}({r['sector_code']}) {source_tag}{stage_tag}")
                print(f"   状态: {r['status']}{push_tag}")
                print(f"   加入: {r['added_date']} | 涨股比: {r['added_ratio']}% | 排名: {r['added_rank']}")
                print(f"   原因: {r['added_reason']}")
                if r['confirmed_date']:
                    print(f"   确认: {r['confirmed_date']} | {r['confirmed_reason']}")
                if r['last_check']:
                    print(f"   最近检查: {r['last_check']} | 涨股比: {r['last_ratio']}% | 上榜: {r['last_list_count']}只 | 信号: {r['last_signal']}")
                print()
    finally:
        conn.close()

def cmd_history(args):
    """查看板块追踪历史"""
    if len(args) < 1:
        print("用法: history <sector_code>")
        return
    code = args[0]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 板块信息
            cur.execute("SELECT * FROM sector_tracker WHERE sector_code = %s", (code,))
            sector = cur.fetchone()
            if not sector:
                print(f"⚠️ 未找到板块 {code}")
                return

            print(f"📊 {sector['sector_name']}({code}) 追踪历史\n")
            print(f"状态: {sector['status']} | 加入: {sector['added_date']}")
            print(f"加入原因: {sector['added_reason']}")
            if sector['confirmed_date']:
                print(f"确认启动: {sector['confirmed_date']} | {sector['confirmed_reason']}")
            print()

            # 检查记录
            cur.execute("""
                SELECT * FROM sector_tracker_checkins
                WHERE sector_code = %s
                ORDER BY check_date DESC
            """, (code,))
            checkins = cur.fetchall()

            if checkins:
                print(f"{'日期':<12} {'涨股比':<8} {'排名':<6} {'5日涨幅':<8} {'20日涨幅':<8} {'上榜':<6} {'信号':<12}")
                print("-" * 70)
                for cr in checkins:
                    ratio_str = f"{cr['ratio']}%" if cr['ratio'] is not None else "N/A"
                    rank_str = f"{cr['rank_no']}/124" if cr['rank_no'] is not None else "N/A"
                    d5_str = f"{cr['d5']:+.2f}" if cr['d5'] is not None else "N/A"
                    d20_str = f"{cr['d20']:+.2f}" if cr['d20'] is not None else "N/A"
                    lc_str = f"{cr['list_count']}只" if cr['list_count'] is not None else "N/A"
                    sig_str = cr['signal_type'] or "N/A"
                    print(f"{cr['check_date']:<12} {ratio_str:<8} {rank_str:<6} {d5_str:<8} {d20_str:<8} {lc_str:<6} {sig_str:<12}")

            # 龙头股
            cur.execute("""
                SELECT * FROM sector_tracker_leaders
                WHERE sector_code = %s
                ORDER BY FIELD(tier, 'Tier1', 'Tier2'), market_cap DESC
            """, (code,))
            leaders = cur.fetchall()

            if leaders:
                print(f"\n🏆 确认龙头股:")
                for l in leaders:
                    print(f"   {l['tier']}: {l['stock_name']}({l['stock_code']}) | 市值{l['market_cap']}亿 | RPS={l['rps_20']}")
                    print(f"      理由: {l['reason']}")
    finally:
        conn.close()

def cmd_confirm(args):
    """确认启动"""
    if len(args) < 2:
        print("用法: confirm <sector_code> <reason>")
        return
    code, reason = args[0], args[1]
    today = datetime.now().strftime("%Y-%m-%d")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sector_tracker SET status = 'confirmed', confirmed_date = %s, confirmed_reason = %s WHERE sector_code = %s",
                (today, reason, code)
            )
            if cur.rowcount > 0:
                print(f"✅ {code} 已确认启动")
                print(f"   日期: {today} | 原因: {reason}")
            else:
                print(f"⚠️ 未找到板块 {code}")
    finally:
        conn.close()

def cmd_fail(args):
    """确认失败"""
    if len(args) < 2:
        print("用法: fail <sector_code> <reason>")
        return
    code, reason = args[0], args[1]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sector_tracker SET status = 'failed' WHERE sector_code = %s",
                (code,)
            )
            if cur.rowcount > 0:
                print(f"❌ {code} 已标记为确认失败")
                print(f"   原因: {reason}")
            else:
                print(f"⚠️ 未找到板块 {code}")
    finally:
        conn.close()

def cmd_add_leader(args):
    """保存龙头股"""
    if len(args) < 7:
        print("用法: add_leader <sector_code> <tier> <stock_code> <stock_name> <market_cap> <rps_20> <reason>")
        return
    code, tier, stock_code, stock_name = args[0], args[1], args[2], args[3]
    market_cap = float(args[4]) if args[4] != 'None' else None
    rps_20 = float(args[5]) if args[5] != 'None' else None
    reason = args[6]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sector_tracker_leaders (sector_code, tier, stock_code, stock_name, market_cap, rps_20, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (code, tier, stock_code, stock_name, market_cap, rps_20, reason)
            )
        print(f"✅ 已保存龙头 {tier}: {stock_name}({stock_code})")
    finally:
        conn.close()

def cmd_pending_push(args):
    """获取待推送列表"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.*,
                    (SELECT check_date FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_check,
                    (SELECT ratio FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_ratio,
                    (SELECT list_count FROM sector_tracker_checkins WHERE sector_code = s.sector_code ORDER BY id DESC LIMIT 1) as last_list_count
                FROM sector_tracker s
                WHERE s.status = 'confirmed' AND s.push_sent = 0
                ORDER BY s.confirmed_date DESC
            """)
            rows = cur.fetchall()

            if not rows:
                print("📭 没有待推送的板块")
                return

            print(f"📨 待推送板块（共 {len(rows)} 个）\n")

            for r in rows:
                print(f"🟢 {r['sector_name']}({r['sector_code']})")
                print(f"   确认日期: {r['confirmed_date']}")
                print(f"   确认原因: {r['confirmed_reason']}")
                print(f"   最近涨股比: {r['last_ratio']}% | 上榜: {r['last_list_count']}只")

                # 查询龙头股
                cur.execute("""
                    SELECT * FROM sector_tracker_leaders
                    WHERE sector_code = %s
                    ORDER BY FIELD(tier, 'Tier1', 'Tier2'), market_cap DESC
                """, (r['sector_code'],))
                leaders = cur.fetchall()
                if leaders:
                    print(f"   龙头股:")
                    for l in leaders:
                        print(f"     {l['tier']}: {l['stock_name']}({l['stock_code']}) | 市值{l['market_cap']}亿 | RPS={l['rps_20']}")
                print()
    finally:
        conn.close()

def cmd_mark_pushed(args):
    """标记已推送"""
    if len(args) < 1:
        print("用法: mark_pushed <sector_code>")
        return
    code = args[0]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE sector_tracker SET push_sent = 1 WHERE sector_code = %s", (code,))
            if cur.rowcount > 0:
                print(f"✅ {code} 已标记为已推送")
            else:
                print(f"⚠️ 未找到板块 {code}")
    finally:
        conn.close()

def main():
    if len(sys.argv) < 2:
        print("用法: sector_tracker.py <command> [args...]")
        print("命令:")
        print("  add <code> <name> <ratio> <rank> <reason> [source] [stage_detail]  - 添加板块到追踪")
        print("  checkin <code> <ratio> <rank> <d5> <d20> <trend> <stage> <flow5> <list_count> [signal_type] [note] [source]")
        print("  list [tracking|trend|all]  - 查看追踪列表（可筛选来源）")
        print("  history <code>          - 查看板块追踪历史")
        print("  confirm <code> <reason> - 确认启动")
        print("  fail <code> <reason>    - 确认失败")
        print("  add_leader <code> <tier> <stock_code> <stock_name> <market_cap> <rps_20> <reason>")
        print("  pending_push            - 获取待推送列表")
        print("  mark_pushed <code>      - 标记已推送")
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "add": cmd_add,
        "checkin": cmd_checkin,
        "list": cmd_list,
        "history": cmd_history,
        "confirm": cmd_confirm,
        "fail": cmd_fail,
        "add_leader": cmd_add_leader,
        "pending_push": cmd_pending_push,
        "mark_pushed": cmd_mark_pushed,
    }

    if cmd in commands:
        commands[cmd](args)
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)

if __name__ == "__main__":
    main()
