#!/usr/bin/env python3
"""
filter_boards_by_trend.py — 16:45 板块龙头分析前置筛选
只保留 active 主线覆盖的板块做龙头分析；fading/peaking/ended 主线板块
（及无主线归属板块）交由 16:00 主线推理 + 16:30 趋势跟随分析，16:45 不覆盖。

用法:
    python3 filter_boards_by_trend.py <trend_follow.md路径> <YYYYMMDD>
输出(stdout): 保留板块列表，每行 "板块名|ptcode|上榜数"（与 run_trend_leaders_daily.sh 的 BOARD_MAP 格式一致）
诊断(stderr): 剔除清单 + 统计
"""
import sys
import re
import json
import pymysql

TREND_FOLLOW = sys.argv[1]
LABEL = sys.argv[2]

# ---------- 1. 解析 trend_follow 板块级走势 ----------
board_trend = {}  # ptcode -> {name, trend, count}
with open(TREND_FOLLOW, encoding="utf-8") as f:
    content = f.read()
# 格式: 工业金属       (pt01801055): 上榜数=20 | 走势=反弹     | 阶段=... | 幅度=..% | 天数=..
pat = re.compile(r"^(\S+?)\s*\((pt\d+)\):\s*上榜数=\s*(\d+)\s*\|\s*走势=(\S+)", re.M)
for m in pat.finditer(content):
    name, code, cnt, trend = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    board_trend[code] = {"name": name, "trend": trend, "count": cnt}

if not board_trend:
    print("ERROR:trend_follow 板块级候选为空（16:30 未输出板块级？）", file=sys.stderr)
    sys.exit(2)

# ---------- 2. 查询 industry_themes 主线→板块状态映射 ----------
date_str = f"{LABEL[:4]}-{LABEL[4:6]}-{LABEL[6:8]}"
theme_sector_status = {}  # ptcode -> {theme, status}
conn = pymysql.connect(host="127.0.0.1", user="wish", password="<DB_PASSWORD>", database="portfolio", charset="utf8mb4")
try:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT theme_name, status, related_sectors FROM industry_themes WHERE theme_date=%s AND is_legacy=0",
            (date_str,),
        )
        for row in cur.fetchall():
            try:
                rs = json.loads(row.get("related_sectors") or "{}")
            except Exception:
                rs = {}
            for s in rs.get("sectors", []) if isinstance(rs, dict) else []:
                if isinstance(s, dict) and s.get("code"):
                    theme_sector_status[s["code"]] = {
                        "theme": row["theme_name"],
                        "status": row["status"],
                    }
finally:
    conn.close()

# ---------- 3. 筛选规则 ----------
# 2026-08-22 二次调整：只聚焦 active 主线板块。
# 保留:
#   - 板块所属主线 status == active（16:45 只分析主线体系内趋势确认的板块）
# 剔除:
#   - 主线 fading/peaking/ended 的板块（航运港口/生物制品等，交由 16:00 主线推理 + 16:30 趋势跟随分析）
#   - 无主线归属的板块（无产业逻辑支撑的反弹，16:45 不覆盖）
# 说明：板块走势↑ 不再作为保留理由——非 active 主线即使走势上涨也不在 16:45 龙头分析范围。
keep = []
dropped = []
for code, info in board_trend.items():
    trend = info["trend"]
    ts = theme_sector_status.get(code)
    tstatus = ts["status"] if ts else None
    ttheme = ts["theme"] if ts else "-"
    if tstatus == "active":
        keep.append((info["count"], info["name"], code, ttheme, tstatus))
    else:
        dropped.append((info["name"], code, trend, ttheme, tstatus))

keep.sort(key=lambda x: -x[0])
print(f"筛选：保留{len(keep)}个，剔除{len(dropped)}个（只保留 active 主线板块）", file=sys.stderr)
for count, name, code, ttheme, tstatus in keep:
    st = tstatus or "无主线"
    print(f"  保留: {name}({code}) 上榜{count} 主线={ttheme}/{st}", file=sys.stderr)
for name, code, trend, ttheme, tstatus in dropped:
    st = tstatus or "无主线"
    print(f"  剔除: {name}({code}) 走势={trend} 主线={ttheme}/{st}", file=sys.stderr)

for count, name, code, ttheme, tstatus in keep:
    print(f"{name}|{code}|{count}")