#!/usr/bin/env python3
"""
verify_theme_sectors.py — 主线 related_sectors 覆盖度一致性核查
================================================================
目的：核查 industry_themes 写库的 related_sectors.sectors 是否每个主线都有
     ≥1 个可拉取成分股的行业板块（16:45 建池/16:30 主线指数合成的覆盖度地基）。
     核查对象为 is_legacy=0 的有效子链（废弃宽链不参与）。

用法：
    python3 verify_theme_sectors.py [YYYY-MM-DD] [YYYY-MM-DD]
    默认核查最近 3 个交易日写库记录（截止今日）；可传起止日期（含两端）。
    输出：stdout 核查报告（markdown 片段）+ 可选 --save 落盘 data/。

判据：
    ✅ 可用 = 该板块代码能通过腾讯 getBoardRankList 拉到 ≥1 只成分股
    ⚠️ 危险 = 主线所有 sectors 均拉不到成分股（16:45 该主线建池为空、16:30 主线指数无成分）
    🔍 缺失 = related_sectors 无 sectors 字段（旧格式，已弃用，见 industry-themes-reasoning 表结构说明）

依赖：
    MySQL portfolio.industry_themes（wish 凭据）
    腾讯 getBoardRankList 接口（成分股，/rank/hs/）
"""

import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

import pymysql

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")


def get_db():
    return pymysql.connect(
        host="127.0.0.1", port=3306, user="wish", password="<DB_PASSWORD>",
        database="portfolio", charset="utf8mb4",
    )


_BOARD_RANK_CACHE = {}


def fetch_board_rank_list(board_code: str) -> list:
    """拉取行业板块成分股列表（腾讯 /rank/hs/getBoardRankList），失败返回 []"""
    if board_code in _BOARD_RANK_CACHE:
        return _BOARD_RANK_CACHE[board_code]
    url = (
        f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
        f"?board_code={board_code}&sort_type=priceRatio&direct=down&count=200"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        data = json.loads(raw)
        rows = (data.get("data") or {}).get("rank_list") or []
    except Exception as e:
        print(f"    ⚠️ 接口异常 {board_code}: {e}", file=sys.stderr)
        rows = []
    _BOARD_RANK_CACHE[board_code] = rows
    return rows


def parse_related(related_json: str):
    """返回 (concepts, sectors)；sectors 为 [{'name','code'}] 或 []"""
    try:
        d = json.loads(related_json)
        if isinstance(d, dict):
            return d.get("concepts", []), d.get("sectors", [])
    except Exception:
        pass
    return [], []


import re as _re


def load_all_theme_constituents():
    """批量读取 theme_constituents（is_current=1），返回 {theme_name: set(stock_code)}。"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT theme_name, stock_code FROM theme_constituents WHERE is_current=1"
            )
            result = defaultdict(set)
            for theme_name, stock_code in cur.fetchall():
                if stock_code:
                    result[theme_name].add(stock_code)
            return result
    except Exception:
        return {}
    finally:
        conn.close()


def parse_plate_concepts(filepath):
    """解析板块统计文件表格1，返回 {概念: [code,...]}（无成分基线时回退用）。"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    table1_match = _re.search(r"## 表格1.*?\n(.+?)(?=\n##|\Z)", content, _re.DOTALL)
    if not table1_match:
        return {}
    concept_stocks = defaultdict(list)
    for line in table1_match.group(1).strip().split("\n"):
        if not line.startswith("|"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 9:
            continue
        code = parts[3]
        tags = [t.strip() for t in _re.split(r"[、,，]", parts[8]) if t.strip()]
        for t in tags:
            concept_stocks[t].append(code)
    return concept_stocks


def check_sectors_consistency(theme_name, sectors, constituents_set, concepts, concept_stocks=None):
    """sectors↔成分一致性校验（T3 写入侧防线）。

    对每个 sector 拉成分股（fetch_board_rank_list），与该链 theme_constituents
    primary 股（is_current=1）做交集。交集为空 → 残留候选（板块成分已漂移）。

    若该链无成分基线，回退用 concepts 概念股集（concept_stocks 提供）。
    若两者均不可用，跳过交集校验，标注"无基线"。

    Returns:
        list — 空列表=全部正常；非空=问题项
        问题项: {"type": "residual"/"no_baseline", "name", "code", "n_constituents", "n_baseline"}
    """
    baseline_set = set(constituents_set) if constituents_set else set()
    if not baseline_set and concept_stocks:
        for c in concepts:
            baseline_set.update(concept_stocks.get(c, []))

    if not baseline_set:
        return [{"type": "no_baseline", "sectors": [
            (s.get("name", "?") if isinstance(s, dict) else str(s)) for s in sectors
        ]}]

    residuals = []
    for sec in sectors:
        if isinstance(sec, str):
            m = _re.match(r"(.+)\(([a-z]+\d+),", sec)
            if m:
                name, code = m.group(1), m.group(2)
            else:
                continue
        else:
            code = sec.get("code", "")
            name = sec.get("name", "")
        if not code:
            continue
        rows = fetch_board_rank_list(code)
        sec_codes = {r.get("code") for r in rows if r.get("code")}
        if not (sec_codes & baseline_set):
            residuals.append({
                "type": "residual", "name": name, "code": code,
                "n_constituents": len(sec_codes), "n_baseline": len(baseline_set),
            })
    return residuals


def fix_residual_sectors(date_str, theme_name, residual_codes):
    """从该主线当日最新记录的 related_sectors.sectors 中移除残留板块（保留 concepts）。

    幂等：若 residual_codes 中无任何板块在 sectors 中（已剔除），返回 False。
    """
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id, related_sectors FROM industry_themes "
                "WHERE theme_date=%s AND theme_name=%s ORDER BY id DESC LIMIT 1",
                (date_str, theme_name),
            )
            row = cur.fetchone()
            if not row:
                return False
            d = json.loads(row["related_sectors"]) if row["related_sectors"] else {}
            sectors = d.get("sectors", [])
            new_sectors = [
                s for s in sectors
                if not (isinstance(s, dict) and s.get("code") in residual_codes)
            ]
            if len(new_sectors) == len(sectors):
                return False
            d["sectors"] = new_sectors
            cur.execute(
                "UPDATE industry_themes SET related_sectors=%s WHERE id=%s",
                (json.dumps(d, ensure_ascii=False), row["id"]),
            )
            return True
    except Exception as e:
        print(f"  ❌ --fix 写库失败({theme_name}): {e}", file=sys.stderr)
        return False
    finally:
        conn.close()


def last_trading_days(n: int = 3) -> list:
    """截止今日往前取 n 个非周末自然日（近似交易日；节假日回溯到有记录即可）"""
    days = []
    d = datetime.now().date()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return days


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    save = "--save" in sys.argv

    fix = "--fix" in sys.argv

    # --date YYYY-MM-DD 覆盖位置参数（T3 验证用）
    date_arg = None
    for i, a in enumerate(sys.argv):
        if a == "--date" and i + 1 < len(sys.argv):
            date_arg = sys.argv[i + 1]

    if date_arg:
        dates = [date_arg, date_arg]
    elif len(args) >= 2:
        dates = [args[0], args[1]]
    elif len(args) == 1:
        dates = [args[0], args[0]]
    else:
        days = last_trading_days(3)
        dates = [days[-1], days[0]]
    print(f"# 主线 sectors 覆盖度核查（{dates[0]} ~ {dates[1]}）")
    print("")
    print(f"> 核查对象：industry_themes related_sectors.sectors 中每个板块代码能否通过腾讯 getBoardRankList 拉到成分股")
    print(f"> 判据：✅可用=≥1只成分股；⚠️危险=主线全部板块拉不到（建池空/指数无成分）；🔍缺失=无 sectors 字段（旧格式已弃用）")
    print("")

    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT theme_date, theme_name, status, related_sectors FROM industry_themes "
                "WHERE theme_date BETWEEN %s AND %s AND is_legacy = 0 ORDER BY theme_date, id",
                (dates[0], dates[1]),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        print("❌ 核查区间无 industry_themes 记录")
        sys.exit(1)

    by_date = defaultdict(list)
    for r in rows:
        by_date[r["theme_date"]].append(r)

    summary_bad = []   # 危险：无可用板块的主线
    summary_missing = []  # 缺失 sectors 字段
    all_sector_codes = set()

    for day in sorted(by_date):
        print(f"## {day}")
        print("")
        print("| 主线 | 状态 | sectors 板块 | 成分股数 | 核查 |")
        print("|:-----|:----|:-----------|:-------:|:----:|")
        for t in by_date[day]:
            concepts, sectors = parse_related(t["related_sectors"])
            if not sectors:
                summary_missing.append((day, t["theme_name"]))
                print(f"| {t['theme_name']} | {t['status']} | 🔍 无 sectors 字段 | - | 🔍 缺失 |")
                continue
            results = []
            n_ok = 0
            for sec in sectors:
                code = ""
                name = ""
                if isinstance(sec, str):
                    import re
                    m = re.match(r"(.+)\(([a-z]+\d+),", sec)
                    if m:
                        name, code = m.group(1), m.group(2)
                else:
                    code = sec.get("code", "")
                    name = sec.get("name", "")
                if not code:
                    continue
                all_sector_codes.add(code)
                constituents = fetch_board_rank_list(code)
                n = len(constituents)
                if n > 0:
                    n_ok += 1
                    results.append(f"{name}={n}只")
                else:
                    results.append(f"{name}=⚠️0只")
            status_icon = "✅" if n_ok >= 1 else ("⚠️" if sectors else "🔍")
            if n_ok == 0:
                summary_bad.append((day, t["theme_name"]))
            print(f"| {t['theme_name']} | {t['status']} | {len(sectors)} 个 | {'/'.join(results)} | {status_icon} |")
        print("")

    # ── sectors↔成分一致性校验 ──
    print("### sectors↔成分一致性")
    print("")
    print("> 判据：板块成分股 ∩ 该链 theme_constituents（is_current=1）交集为空 → 残留板块（成分已漂移）")
    print("> 无成分基线时回退 concepts 概念股；两者均不可用则标注「无基线」")
    print("")

    constituents_map = load_all_theme_constituents()
    fix_count = 0

    for day in sorted(by_date):
        day_str = str(day)
        date_compact = day_str.replace("-", "")
        plate_file = os.path.join(DATA_DIR, "plate", f"plate_{date_compact}.md")
        concept_stocks = parse_plate_concepts(plate_file)

        for t in by_date[day]:
            concepts, sectors = parse_related(t["related_sectors"])
            if not sectors:
                continue
            theme_name = t["theme_name"]
            constituents_set = constituents_map.get(theme_name, set())
            issues = check_sectors_consistency(
                theme_name, sectors, constituents_set, concepts, concept_stocks
            )
            if not issues:
                print(f"- {theme_name} ({day_str}): ✅ 全部正常")
            elif issues[0].get("type") == "no_baseline":
                print(f"- {theme_name} ({day_str}): 🔍 无基线（池外新链，跳过交集校验）")
            else:
                residual_strs = [
                    f"{i['name']}（成分{i['n_constituents']}只/交集0/{i['n_baseline']}）"
                    for i in issues if i.get("type") == "residual"
                ]
                print(f"- {theme_name} ({day_str}): ⚠️ 残留板块 {'; '.join(residual_strs)}")
                if fix:
                    residual_codes = {i["code"] for i in issues if i.get("type") == "residual"}
                    removed = fix_residual_sectors(day_str, theme_name, residual_codes)
                    if removed:
                        fix_count += 1
                        names = [i["name"] for i in issues if i.get("type") == "residual"]
                        print(f"  → ✅ --fix 已剔除: {', '.join(names)}")
                    else:
                        print(f"  → ℹ️ 无需剔除（已无残留或记录不存在）")

    if fix:
        print(f"\n> --fix 完成：共剔除 {fix_count} 条主线的残留板块")
    print("")

    print("---")
    print("")
    print("## 汇总")
    print("")
    print(f"- 核查主线-日内记录：{len(rows)} 条 | 涉及板块代码：{len(all_sector_codes)} 个")
    if summary_missing:
        print("- 🔍 **无 sectors 字段（旧格式，需迁移补全）：** " + "；".join(f"{d} {n}" for d, n in summary_missing))
    else:
        print("- 🔍 无 sectors 缺失：全部记录均带 sectors 字段 ✅")
    if summary_bad:
        print("- ⚠️ **无可用板块（建池空风险）：** " + "；".join(f"{d} {n}" for d, n in summary_bad))
    else:
        print("- ⚠️ 无危险主线：每条主线均 ≥1 个板块可拉取成分股 ✅")

    if save:
        fname = f"{DATA_DIR}/sectors_coverage_check_{dates[1].replace('-', '')}.md"
        # 本脚本 print 到 stdout；落盘需重定向。--save 仅做占位说明
        print(f"\n> 提示：落盘请用 `python3 verify_theme_sectors.py {dates[0]} {dates[1]} > {fname}`（脚本本身不写文件）")


if __name__ == "__main__":
    main()
