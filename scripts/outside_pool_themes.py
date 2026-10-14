#!/usr/bin/env python3
"""
outside_pool_themes.py — 池外板块主线归纳（18:30 环节）
================================================================
在 17:30 faded_theme_monitor 落库 sector_rank_daily + 18:00 监控池分析之后运行。

定位：对**池外板块**（不在 faded_theme_watch 监控池的行业）做主线归纳，
识别全A排行中正在启动但尚未被监控的产业方向，归纳为「要监控追踪的候选主线」。

产业归属判定：**公司主营业务语义推导**。
候选板块代表股主营（prepare_pool_derivation.py 取数）→ Agent 语义推理
（推理规范 `设计文档/20261009_池外主营推导_推理规范.md`）→ derived_theme。
本脚本消费推理结果（--cluster-from）生成候选主线。

  【维度A】当日池外横截面 zdf 分位准入
  【维度B】低位扩散：rank_d20 分位 + zgb_ratio 分位 + zdf 非负
  【补充 b方案】板块统计文件 plate_*.md 表格2 上榜数 + 表格1 概念标签命中，辅助确认

评分 = n_dim_b×3.0 + n_dim_a×1.5 + 涨股比/20 + min(量比,3) + min(上榜数/5,3)
强度：quality_sum 的当日候选池分位达 Q_TOP 为 strong、Q_MID 为 moderate、其余 weak

成色评级 track_level（对齐 16:00 industry-themes-reasoning 2.1 三层过滤）：
  硬门槛（一票否决 → filtered）：覆盖板块数 <2（单板块 = 标签现象，非行业现象）
  成色评分 quality（5维×0~3，全部按当日候选池分位定档）：
    产业纵深（板块数）、普涨度（平均涨股比）、趋势持续性（平均20日涨幅）、资金确认（平均量比）
    概念确认：1（概念加分已退役，归属由主营推导判定，不再叠加静态概念映射）
  track_level：quality_sum 分位达 Q_TOP → main；达 Q_MID → watch；否则 filtered；硬门槛不过直接 filtered

口径：全部客观量价（sector_rank_daily 落库数据 + 板块统计文件），禁用主力资金等推算指标。

输出：
  data/outside_pool_themes_YYYYMMDD.md    报告
  data/outside_pool_themes_YYYYMMDD.json  结构化结论（供 18:45 outside_pool_leaders.py 读取）

用法：
  python3 outside_pool_themes.py <YYYYMMDD> --cluster-from data/pool_derivation_result_YYYYMMDD.json
  bash scripts/run_outside_pool_themes_cron.sh [YYYYMMDD]   # cron/nohup 入口
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional

# ================= 路径配置（生产：/home/agentuser/data/定时任务打包） =================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
DATA_DIR = os.path.join(WORKSPACE, "data")
PLATE_DIR = os.path.join(WORKSPACE, "data", "plate")

# 数据库中 sector_rank_daily 的 MySQL 配置
DB_ARGS = ['mysql', '-u', 'wish', '-p<DB_PASSWORD>', 'portfolio', '-N', '-e']

# 监控池板块代码（从 faded_theme_watch 读取，非 dropped 状态均在池）
POOL_CODES_SQL = (
    "SELECT DISTINCT related_sectors FROM faded_theme_watch "
    "WHERE watch_status IN ('watching','reviving','revived')"
)

# 分位参数，20 交易日均值校准，非市场常量
Q_ZGB_OUTSIDE = 0.72
Q_BREADTH_HI = 0.88
Q_BREADTH_LO = 0.72
Q_TREND_HI = 0.96
Q_TREND_LO = 0.70
Q_CAP_HI = 0.87
Q_CAP_LO = 0.64
Q_DEPTH_HI = 0.80
Q_DEPTH_LO = 0.50
Q_TOP = 0.67
Q_MID = 0.33

# 池外归属 source 优先级（簇级聚合：derived > independent；被主线吸收板块不进入聚类）
_SOURCE_PRIORITY = {"derived": 0, "independent": 1}


# ================= 数据库工具 =================

def mysql_q(sql: str) -> List[str]:
    """执行 MySQL 查询，返回行列表"""
    try:
        proc = subprocess.run(DB_ARGS + [sql], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            print(f"  ⚠️ MySQL 查询失败: {proc.stderr[:200]}", file=sys.stderr)
            return []
        return [line for line in proc.stdout.strip().split("\n") if line.strip()]
    except Exception as e:
        print(f"  ⚠️ MySQL 异常: {e}", file=sys.stderr)
        return []


def percentile_rank(value, values) -> float:
    """当日候选池升序分位，输入集合内 x 的位置映射到 0–1。"""
    valid = [v for v in values if v is not None]
    if value is None or not valid:
        return 0.0
    if len(valid) == 1:
        return 1.0
    return (sum(v <= value for v in valid) - 1) / (len(valid) - 1)


# ================= 数据加载 =================

def load_latest_rank_snapshot() -> List[dict]:
    """从 sector_rank_daily 读最新一日全A排行快照（17:30 faded_theme_monitor 落库）。
    返回 rank_daily 升序（=当日涨幅降序）的行列表。
    """
    sql = ("SELECT rank_date, code, name, zdf, zdf_d5, zdf_d20, zgb_ratio, lb, rank_daily, rank_d20 "
           "FROM sector_rank_daily WHERE rank_date=(SELECT MAX(rank_date) FROM sector_rank_daily) "
           "ORDER BY rank_daily")
    rows = []
    for line in mysql_q(sql):
        parts = line.split("\t")
        if len(parts) < 10:
            continue

        def _f(v):
            try:
                return float(v)
            except (ValueError, TypeError):
                return None

        def _i(v):
            try:
                return int(float(v))
            except (ValueError, TypeError):
                return None

        rows.append({
            "date": parts[0], "code": parts[1], "name": parts[2],
            "zdf": _f(parts[3]), "zdf_d5": _f(parts[4]), "zdf_d20": _f(parts[5]),
            "zgb_ratio": _f(parts[6]), "lb": _f(parts[7]),
            "rank_daily": _i(parts[8]), "rank_d20": _i(parts[9]),
        })
    return rows


def load_pool_codes() -> set:
    """从 faded_theme_watch 读取监控池板块代码"""
    codes = set()
    for line in mysql_q(POOL_CODES_SQL):
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                for s in data.get("sectors", []):
                    if isinstance(s, dict) and s.get("code"):
                        codes.add(s["code"])
            elif isinstance(data, list):
                for s in data:
                    if isinstance(s, str):
                        m = re.match(r'.+\\(([a-z]+\\d+),', s)
                        if m:
                            codes.add(m.group(1))
                    elif isinstance(s, dict) and s.get("code"):
                        codes.add(s["code"])
        except (json.JSONDecodeError, AttributeError):
            pass
    return codes


def load_industry_themes(date_str: str) -> List[dict]:
    """从 industry_themes 表读取当日主线（用 pymysql 直连避免字符集问题）

    P2 §2.5：同时读取 parent_theme（链族名）供子链命名空间对齐。
    """
    themes = []
    try:
        import pymysql
        conn = pymysql.connect(host="127.0.0.1", port=3306, user="wish", password="<DB_PASSWORD>",
                               database="portfolio", charset="utf8mb4")
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT theme_name, status, related_sectors, parent_theme FROM industry_themes WHERE theme_date = %s AND is_legacy = 0",
                (date_str,)
            )
            for row in cur.fetchall():
                name = row["theme_name"]
                if "?" in name:
                    continue
                themes.append({
                    "theme_name": name,
                    "status": row["status"],
                    "related_sectors": row.get("related_sectors", ""),
                    "parent_theme": row.get("parent_theme"),
                })
        conn.close()
    except Exception as e:
        print(f"    ⚠️ pymysql 读取 industry_themes 失败: {e}", file=sys.stderr)
    return themes


# ===== 同名 sectors 冲突校验（主营推导候选同样需要：杜绝同名不同板块集合） =====

def resolve_theme_conflict(theme_name: str, secs: List[dict], current_themes: List[dict]) -> tuple:
    """同名 sectors 冲突校验，返回 (final_name, conflict, conflict_parent, is_existing)。
    - 候选 theme_name 在当日主线中存在 → 对比 sectors 代码集合
    - 候选 ⊆ 主线 → 复用原名（is_existing=True）
    - 存在候选板块不在主线 sectors → conflict：
        差异板块 1~2 个 → f"{theme}-{首差异板块名不含Ⅱ}链"
        差异板块 ≥3 个   → f"{theme}-池外链"
    """
    if not theme_name:
        return theme_name, False, None, False
    sec_codes = {s["code"] for s in secs if s.get("code")}
    code2name = {s["code"]: s.get("name", "") for s in secs}
    for ct in current_themes:
        if ct.get("theme_name") != theme_name:
            continue
        mainline_codes = ct.get("sec_codes", set())
        diff = sec_codes - mainline_codes
        if not diff:
            return theme_name, False, None, True
        diff_names = [code2name[c] for c in [s["code"] for s in secs] if c in diff]
        # 同名前缀取短名（去"产业链"后缀），差异板块名去"Ⅱ"
        short_theme = theme_name[:-3] if theme_name.endswith("产业链") else theme_name
        if len(diff) <= 2:
            first = (diff_names[0] if diff_names else "").replace("Ⅱ", "").strip()
            final = f"{short_theme}-{first}链"
        else:
            final = f"{short_theme}-池外链"
        return final, True, theme_name, False
    # parent_theme 同名分支（P4 后主线全子链化：宽链名只存在于 parent_theme 字段）
    chain_union = set()
    for ct in current_themes:
        if ct.get("parent_theme") != theme_name:
            continue
        chain_union |= ct.get("sec_codes", set())
    if chain_union:
        diff = sec_codes - chain_union
        if not diff:
            return theme_name, False, None, True
        diff_names = [code2name[c] for c in [s["code"] for s in secs] if c in diff]
        short_theme = theme_name[:-3] if theme_name.endswith("产业链") else theme_name
        if len(diff) <= 2:
            first = (diff_names[0] if diff_names else "").replace("Ⅱ", "").strip()
            final = f"{short_theme}-{first}链"
        else:
            final = f"{short_theme}-池外链"
        return final, True, theme_name, False
    return theme_name, False, None, False


def parse_plate_table2(filepath: str) -> Dict[str, int]:
    """解析板块统计文件表格2（行业板块上榜数），返回 {板块代码: 上榜数}"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"## 表格2.*?\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
    if not m:
        return {}
    result = {}
    for line in m.group(1).strip().split("\n"):
        if not line.startswith("|"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 5:
            # | # | 行业板块 | 代码 | 上榜数 | ...
            code = parts[3]
            count_str = parts[4]
            try:
                count = int(count_str)
            except ValueError:
                continue
            result[code] = count
        elif len(parts) >= 4:
            # 无代码格式：| # | 行业板块 | 上榜数 | ...
            name = parts[2]
            count_str = parts[3]
            try:
                count = int(count_str)
            except ValueError:
                continue
            result[name] = count
    return result


def parse_plate_table1_concepts(filepath: str) -> Dict[str, int]:
    """解析板块统计文件表格1（概念标签），返回 {概念标签: 上榜股数}"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"## 表格1.*?\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
    if not m:
        return {}
    result = defaultdict(int)
    for line in m.group(1).strip().split("\n"):
        if not line.startswith("|"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 10:
            continue
        concepts_str = parts[8]
        for tag in re.split(r"[、,，]", concepts_str):
            tag = tag.strip()
            if tag:
                result[tag] += 1
    return dict(result)


# ================= 评分/成色/冲突（主营推导消费共用后半段） =================

def score_and_finalize(clustered: dict, rank_snapshot: List[dict], pool_codes: set,
                       plate_t2: Dict[str, int],
                       existing_themes: List[dict],
                       secondary_votes: dict = None) -> List[dict]:
    """评分/成色/冲突（数据已由主营推导组装为 clustered={theme_name: [sectors]}）。

    P2 §2.5：
    - conflict 判定按 sectors 交集（已有 resolve_theme_conflict），非概念集
    - 次属半票加权（B2①）：primary_vote（主属全票）+ secondary_votes（次属半票）→ weighted_vote
    计算评分/quality/track_level/conflict → 排序返回。
    """
    secondary_votes = secondary_votes or {}
    # 当日主线 sec_codes 视图（用于同名冲突校验）
    mainline_views = []
    for t in existing_themes:
        codes = set()
        rs = t.get("related_sectors", "")
        try:
            data = json.loads(rs) if rs else {}
            for sec in data.get("sectors", []):
                if isinstance(sec, dict) and sec.get("code"):
                    codes.add(sec["code"])
        except (json.JSONDecodeError, TypeError):
            pass
        mainline_views.append({"theme_name": t["theme_name"], "sec_codes": codes,
                               "status": t.get("status"), "parent_theme": t.get("parent_theme")})

    # 计算每条候选主线的评分与指标，分位分布基于当日候选池
    results = []
    metric_rows = []
    for theme_name, secs in clustered.items():
        if not secs:
            continue

        # 判断是否已在监控池（通过板块代码）
        is_pool = any(s["code"] in pool_codes for s in secs)

        # 簇级 source：取最高优先级成员（derived > independent）
        best = min(secs, key=lambda s: _SOURCE_PRIORITY.get(s.get("_attr_source", "independent"), 99))
        theme_source = best.get("_attr_source", "independent")
        theme_status = best.get("_status_constraint")

        # 同名 sectors 冲突校验
        final_name, conflict, conflict_parent, is_existing = resolve_theme_conflict(
            theme_name, secs, mainline_views)

        # 计算平均指标
        avg_zdf = sum(s["zdf"] for s in secs if s["zdf"] is not None) / max(len([s for s in secs if s["zdf"] is not None]), 1)
        avg_zdf_d5 = sum(s["zdf_d5"] for s in secs if s["zdf_d5"] is not None) / max(len([s for s in secs if s["zdf_d5"] is not None]), 1)
        avg_zdf_d20 = sum(s["zdf_d20"] for s in secs if s["zdf_d20"] is not None) / max(len([s for s in secs if s["zdf_d20"] is not None]), 1)
        avg_zgb = sum(s["zgb_ratio"] for s in secs if s["zgb_ratio"] is not None) / max(len([s for s in secs if s["zgb_ratio"] is not None]), 1)
        avg_lb = sum(s["lb"] for s in secs if s["lb"] is not None) / max(len([s for s in secs if s["lb"] is not None]), 1)

        # 维度B板块数（高权重）
        n_dim_b = sum(1 for s in secs if s.get("_source") == "dim_b")
        n_dim_a = sum(1 for s in secs if s.get("_source") == "dim_a")

        # 板块统计文件上榜数（b方案）
        plate_count = sum(plate_t2.get(s["code"], 0) for s in secs)

        # P2 §2.5 B2①：加权投票（主属全票 + 次属半票）
        primary_vote = sum(
            (b.get("vote") or {}).get(theme_name, 0)
            for b in secs if isinstance(b.get("vote"), dict)
        )
        sec_vote = secondary_votes.get(theme_name, 0.0)
        weighted_vote = primary_vote + sec_vote

        # 综合评分（概念命中加分已退役）
        score = 0.0
        score += n_dim_b * 3.0
        score += n_dim_a * 1.5
        if avg_zgb is not None:
            score += avg_zgb / 20.0
        if avg_lb is not None:
            score += min(avg_lb, 3.0)
        score += min(plate_count / 5.0, 3.0)
        # P2 §2.5 B2①：加权投票轻量加分（主属全票+次属半票，上限 2.0）
        score += min(weighted_vote / 10.0, 2.0)

        n_sectors = len(secs)
        hard_pass = n_sectors >= 2
        metric_rows.append({
            "result_index": len(results),
            "n_sectors": n_sectors,
            "avg_zgb": avg_zgb,
            "avg_zdf_d20": avg_zdf_d20,
            "avg_lb": avg_lb,
        })

        results.append({
            "theme_name": final_name,
            "sectors": secs,
            "source": theme_source,
            "status_constraint": theme_status,
            "conflict": conflict,
            "conflict_parent": conflict_parent,
            "strength": "weak",
            "track_level": "filtered",
            "quality": 0,
            "quality_detail": {},
            "hard_pass": hard_pass,
            "avg_zdf": avg_zdf,
            "avg_zdf_d5": avg_zdf_d5,
            "avg_zdf_d20": avg_zdf_d20,
            "avg_zgb_ratio": avg_zgb,
            "avg_lb": avg_lb,
            "plate_count": plate_count,
            "score": score,
            "is_pool": is_pool,
            "is_existing_theme": is_existing,
            "weighted_vote": round(weighted_vote, 1),
        })

    depth_values = [row["n_sectors"] for row in metric_rows]
    breadth_values = [row["avg_zgb"] for row in metric_rows]
    trend_values = [row["avg_zdf_d20"] for row in metric_rows]
    capital_values = [row["avg_lb"] for row in metric_rows]
    quality_values = []
    quality_details = []
    for row in metric_rows:
        q_depth = (3 if percentile_rank(row["n_sectors"], depth_values) >= Q_DEPTH_HI
                   else 2 if percentile_rank(row["n_sectors"], depth_values) >= Q_DEPTH_LO else 1)
        breadth_pct = percentile_rank(row["avg_zgb"], breadth_values)
        q_breadth = (3 if breadth_pct >= Q_BREADTH_HI
                     else 2 if breadth_pct >= Q_BREADTH_LO else 1)
        trend_pct = percentile_rank(row["avg_zdf_d20"], trend_values)
        q_trend = (3 if trend_pct >= Q_TREND_HI
                   else 2 if trend_pct >= Q_TREND_LO else 1)
        capital_pct = percentile_rank(row["avg_lb"], capital_values)
        q_capital = (3 if capital_pct >= Q_CAP_HI
                     else 2 if capital_pct >= Q_CAP_LO else 1)
        q_concept = 1
        quality_values.append(q_depth + q_breadth + q_trend + q_capital + q_concept)
        quality_details.append({
            "depth": q_depth, "breadth": q_breadth, "trend": q_trend,
            "capital": q_capital, "concept": q_concept,
        })

    for row, quality, quality_detail in zip(metric_rows, quality_values, quality_details):
        result = results[row["result_index"]]
        score_pct = percentile_rank(quality, quality_values)
        strength = ("strong" if score_pct >= Q_TOP
                    else "moderate" if score_pct >= Q_MID else "weak")
        track_level = ("main" if score_pct >= Q_TOP
                       else "watch" if score_pct >= Q_MID else "filtered")
        if not result["hard_pass"]:
            track_level = "filtered"

        result["strength"] = strength
        result["track_level"] = track_level
        result["quality"] = quality
        result["quality_pct"] = score_pct
        result["quality_detail"] = quality_detail

    results.sort(key=lambda x: (x["quality_pct"], x["score"]), reverse=True)
    return results


def consume_derivation_result(result: dict, rank_snapshot: List[dict], pool_codes: set,
                              plate_t2: Dict[str, int],
                              existing_themes: List[dict]) -> tuple:
    """消费 Agent 推理结果（pool_derivation_result_*.json）生成候选主线。

    P2 §2.5 改造：
    1) 主线优先：板块 code 已被当日主线 related_sectors 收录 → 从候选池移除。
       子链命名空间对齐：derived_theme 与主线 theme_name 或 parent_theme 同名 → 直接吸收（不产生 conflict）。
    2) 其余板块按 derived_theme 聚类（derived/independent）。
    3) 次属半票加权（B2①）：board 的 vote dict 中 derived_theme 为主属（全票 1.0），
       其余 theme 为次属（半票 0.5）→ 消费侧计算 weighted_vote。
    4) 调用 score_and_finalize(...) 计算评分/成色/冲突（sectors/成分交集判定）。
    5) 每条 result 附加 derivation 明细。
    返回 (results, absorbed_boards)。
    """
    _INDEP = {None, "", "独立", "independent", "Independent", "INDEPENDENT"}
    boards = result.get("boards", [])

    # 当日主线 sector_code → theme_name 视图 + parent_theme 视图（子链命名空间对齐）
    mainline_code_map = {}
    mainline_names = set()  # 所有主线 theme_name + parent_theme（链族名）
    for t in existing_themes:
        rs = t.get("related_sectors", "")
        try:
            rs_data = json.loads(rs) if rs else {}
        except (json.JSONDecodeError, TypeError):
            rs_data = {}
        for sec in rs_data.get("sectors", []):
            if isinstance(sec, dict) and sec.get("code"):
                mainline_code_map[sec["code"]] = t["theme_name"]
        mainline_names.add(t["theme_name"])
        if t.get("parent_theme"):
            mainline_names.add(t["parent_theme"])

    # 1) 组装 clustered（主线优先移除 → derived_theme 聚类 → 独立候选）
    clustered = defaultdict(list)
    absorbed_boards = []
    for b in boards:
        code = b.get("code")
        derived = b.get("derived_theme")
        # 主线优先：板块 code 已被当日主线 sectors 收录 → 从候选池移除
        if code in mainline_code_map:
            mainline_theme = mainline_code_map[code]
            b["mainline_absorbed"] = True
            b["mainline_theme"] = mainline_theme
            # 子链命名空间对齐：derived_theme 与主线同名或 parent_theme 同名 → 直接吸收，不产生 conflict
            if derived not in _INDEP and derived != mainline_theme and derived not in mainline_names:
                b["derivation_divergence"] = True
                b["agent_theme_orig"] = derived
            absorbed_boards.append(b)
        elif derived in _INDEP:
            b["_attr_source"] = "independent"
            b["_status_constraint"] = None
            clustered[b.get("name", b.get("code", ""))].append(b)
        else:
            b["_attr_source"] = "derived"
            b["_status_constraint"] = None
            clustered[derived].append(b)

    # 2) 次属半票加权（B2①）：计算每条候选主线的 weighted_vote
    #    主属（derived_theme == 候选名）→ 全票 1.0；次属（vote 含候选名但 derived 不同）→ 半票 0.5
    cluster_names = set(clustered.keys())
    secondary_votes = defaultdict(float)
    for b in boards:
        if b.get("mainline_absorbed"):
            continue
        derived = b.get("derived_theme")
        vote = b.get("vote") or {}
        if not isinstance(vote, dict):
            continue
        for vtheme, vcount in vote.items():
            if vtheme in _INDEP or vtheme not in cluster_names:
                continue
            if vtheme == derived:
                continue  # 主属已计入聚类，不重复
            # 次属半票
            secondary_votes[vtheme] += vcount * 0.5

    # 3) 评分/成色/冲突（传入 secondary_votes 供加权）
    results = score_and_finalize(dict(clustered), rank_snapshot, pool_codes,
                                 plate_t2, existing_themes,
                                 secondary_votes=dict(secondary_votes))

    # 4) 附加 derivation 明细
    for r in results:
        board_votes = {}
        stocks_summary = []
        for s in r.get("sectors", []):
            bname = s.get("name", "")
            vote = s.get("vote")
            if vote:
                board_votes[bname] = vote
            for stk in (s.get("stocks") or []):
                stocks_summary.append({
                    "code": stk.get("code", ""),
                    "name": stk.get("name", ""),
                    "theme": stk.get("theme", ""),
                    "confidence": stk.get("confidence", ""),
                    "evidence": stk.get("evidence", ""),
                    "note": stk.get("note", ""),
                })
        r["derivation"] = {
            "board_votes": board_votes,
            "stocks": stocks_summary,
            "weighted_vote": r.get("weighted_vote", 0.0),
        }

    return results, absorbed_boards


# ================= 报告渲染 =================

def fmt_pct(v, digits=2, plus=True):
    if v is None:
        return "N/A"
    s = f"{v:.{digits}f}%"
    return f"+{s}" if plus and v > 0 else s


def track_badge(t: dict) -> str:
    """成色评级徽标（对齐 16:00 三层过滤）"""
    lv = t.get("track_level", "filtered")
    q = t.get("quality", 0)
    if lv == "main":
        return f"🟢主线({q},P{t.get('quality_pct', 0):.2f})"
    if lv == "watch":
        return f"🟡观察({q},P{t.get('quality_pct', 0):.2f})"
    return f"⚫过滤({q},P{t.get('quality_pct', 0):.2f})"


_SOURCE_CN = {"derived": "推导", "independent": "独立"}


def _source_label(t: dict) -> str:
    """归属源标签：二值中文 + 维度A/B 板块数"""
    cn = _SOURCE_CN.get(t.get("source", ""), t.get("source", ""))
    n_a = sum(1 for s in t.get("sectors", []) if s.get("_source") == "dim_a")
    n_b = sum(1 for s in t.get("sectors", []) if s.get("_source") == "dim_b")
    return f"{cn}(A{n_a}+B{n_b})"


def _attr_note(t: dict) -> str:
    """口径标注：conflict 消歧（主线优先分歧见独立小节「已归主线板块」）"""
    notes = []
    if t.get("conflict"):
        notes.append("※池外口径与主线同名不一致")
    return "；".join(notes)


def _absorbed_section(absorbed_boards: List[dict]) -> str:
    """主线优先吸收板块小节：已被当日主线收录的候选板块（分歧记录供 16:00 复核）"""
    if not absorbed_boards:
        return ""
    out = ["---", "", "## 🔄 已归主线板块（主线优先，设计 3.5）", ""]
    out.append("以下候选板块已被当日主线 sectors 收录，属主线成员，18:30 **不再输出为池外候选**；")
    out.append("Agent 主营推导与主线归属不同者记录分歧，供 16:00 主线推理复核：")
    out.append("")
    out.append("| 板块 | 主线归属 | Agent 推导 | 分歧? |")
    out.append("|:---|:---|:---|:---:|")
    for b in absorbed_boards:
        name = b.get("name", b.get("code", ""))
        mainline = b.get("mainline_theme", "")
        orig = b.get("agent_theme_orig", "")
        div = "🔄 分歧" if b.get("derivation_divergence") else ""
        div_text = f"{orig}（{div}）" if b.get("derivation_divergence") else ("同主线" if orig else "-")
        out.append(f"| {name} | {mainline} | {div_text} | {'🔄' if b.get('derivation_divergence') else ''} |")
    out.append("")
    return "\n".join(out)


def render_report(date_compact: str, outside_themes: List[dict],
                  rank_snapshot: List[dict], pool_codes: set,
                  plate_t2: Dict[str, int], concepts: Dict[str, int],
                  derivation_unavailable: bool = False,
                  absorbed_boards: List[dict] = None) -> str:
    """渲染任务1报告（主线归纳）"""
    out = []
    snap_date = rank_snapshot[0]["date"] if rank_snapshot else date_compact
    # 行业总数动态化（替代硬编码 124/62）
    rank_total = len(rank_snapshot) if rank_snapshot else 0
    rank_top_half = rank_total // 2
    if date_compact != datetime.now().strftime("%Y%m%d"):
        run_label = f"手动补跑 {date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    else:
        run_label = "自动执行"

    out.append(f"# 🌐 池外板块主线归纳报告 {date_compact}")
    out.append("")
    out.append(f"> 生成时间：{run_label}")
    out.append(f"> 环节：18:30")
    out.append(f"> 数据源：sector_rank_daily 全A排行快照（{snap_date}）+ 板块统计文件 plate_{date_compact}.md")
    out.append("> 口径：池外横截面分位；维度A按 zdf 分位，维度B按低位扩散分位；成色分项按当日候选池分位")
    out.append("> 聚类：主营推导（公司主营业务语义推导→候选主线）；同名 sectors 冲突校验强制改名")
    if derivation_unavailable:
        out.append("> ⚠️ 推导不可用：主营数据失败/推理环节异常，候选全部独立观察")
    out.append("")

    # ========== 数据概览 ==========
    out.append("## 📊 全A排行快照概览")
    out.append("")
    valid = [r for r in rank_snapshot if r["zdf"] is not None]
    outside_snapshot = [r for r in rank_snapshot if r["code"] not in pool_codes]
    outside_zgb_values = [r["zgb_ratio"] for r in outside_snapshot if r.get("zgb_ratio") is not None]
    dim_b_count = sum(1 for r in outside_snapshot
                      if (r.get("rank_d20") or 999) > rank_top_half
                      and r.get("zgb_ratio") is not None
                      and percentile_rank(r["zgb_ratio"], outside_zgb_values) >= Q_ZGB_OUTSIDE)
    out.append(f"- 全A行业总数：{len(rank_snapshot)}")
    out.append(f"- 当日涨幅TOP10（维度A）：{len(valid[:10])} 个行业")
    out.append(f"- 低20日位次+高涨股比（维度B）：{dim_b_count} 个行业")
    out.append(f"- 监控池板块数：{len(pool_codes)}")
    out.append(f"- 板块统计文件上榜数：{sum(plate_t2.values())} 只")
    out.append("")

    # ========== 维度A：当日涨幅TOP10 ==========
    out.append("## 📈 维度A：当日涨幅TOP10行业")
    out.append("")
    out.append("| # | 行业 | 代码 | 当日% | 5日% | 20日% | 涨股比 | 量比 | 池内? |")
    out.append("|--:|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
    pool_flag = lambda code: "池内" if code in pool_codes else "池外"
    for rk, r in enumerate(valid[:10], 1):
        z5 = f"{r['zdf_d5']:+.1f}" if r.get("zdf_d5") is not None else "N/A"
        z20 = f"{r['zdf_d20']:+.1f}" if r.get("zdf_d20") is not None else "N/A"
        zg = f"{r['zgb_ratio']:.0f}%" if r.get("zgb_ratio") is not None else "N/A"
        lb = f"{r['lb']:.1f}" if r.get("lb") is not None else "N/A"
        out.append(f"| {rk} | {r['name']} | {r['code']} | {r['zdf']:+.1f} | {z5} | {z20} | {zg} | {lb} | {pool_flag(r['code'])} |")
    out.append("")

    # ========== 维度B：低20日位次+高涨股比 ==========
    dim_b = [r for r in outside_snapshot
             if (r.get("rank_d20") or 999) > rank_top_half
             and r.get("zgb_ratio") is not None
             and percentile_rank(r["zgb_ratio"], outside_zgb_values) >= Q_ZGB_OUTSIDE]
    if dim_b:
        out.append("## 🔄 维度B：低20日位次+高涨股比（潜在轮动起点）")
        out.append("")
        out.append("| 行业 | 代码 | 20日位次 | 20日% | 当日% | 涨股比 | 量比 | 池内? |")
        out.append("|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
        for r in dim_b[:10]:
            z20 = f"{r['zdf_d20']:+.1f}" if r.get("zdf_d20") is not None else "N/A"
            zd = f"{r['zdf']:+.1f}" if r.get("zdf") is not None else "N/A"
            lb = f"{r['lb']:.1f}" if r.get("lb") is not None else "N/A"
            out.append(f"| {r['name']} | {r['code']} | {r['rank_d20']}/{rank_total} | {z20} | {zd} | {r['zgb_ratio']:.0f}% | {lb} | {pool_flag(r['code'])} |")
        out.append("")
    else:
        out.append("## 🔄 维度B：低20日位次+高涨股比\n\n- 无（低位且涨股比分位达阈值的池外行业未出现）\n")

    # ========== 板块统计文件上榜数 ==========
    if plate_t2:
        out.append("## 📋 板块统计文件上榜数（b方案补充）")
        out.append("")
        out.append("| 板块代码 | 上榜数 | 池内? |")
        out.append("|:---|:---:|:---:|")
        for code, cnt in sorted(plate_t2.items(), key=lambda x: x[1], reverse=True)[:15]:
            out.append(f"| {code} | {cnt} | {pool_flag(code)} |")
        out.append("")

    # ========== 概念标签命中 ==========
    if concepts:
        out.append("## 🏷️ 概念标签上榜统计")
        out.append("")
        out.append("| 概念标签 | 上榜股数 |")
        out.append("|:---|:---:|")
        for tag, cnt in sorted(concepts.items(), key=lambda x: x[1], reverse=True)[:15]:
            out.append(f"| {tag} | {cnt} |")
        out.append("")

    # ========== 任务1：候选主线归纳 ==========
    out.append("---")
    out.append("")
    out.append("## 🏭 池外板块 → 候选主线归纳")
    out.append("")

    if not outside_themes:
        out.append("**无候选主线（池外板块未形成有效聚类）。**")
        out.append("")
    else:
        strong = [t for t in outside_themes if t["strength"] == "strong"]
        moderate = [t for t in outside_themes if t["strength"] == "moderate"]
        weak = [t for t in outside_themes if t["strength"] == "weak"]

        if strong:
            out.append("### 🟢 强信号候选主线")
            out.append("")
            out.append("| 候选主线 | 覆盖板块数 | 归属源 | 平均当日% | 平均20日% | 平均涨股比 | 平均量比 | 上榜数 | 评分 | 成色 | 已入池? | 已有主线? | 口径标注 |")
            out.append("|:--------|:--------:|:--------|:--------:|:--------:|:--------:|:--------:|:-----:|:----:|:----:|:------:|:--------:|:--------|")
            for t in strong:
                out.append(f"| {t['theme_name']} | {len(t['sectors'])} | {_source_label(t)} | {t['avg_zdf']:+.1f}% | {fmt_pct(t['avg_zdf_d20'])} | {t['avg_zgb_ratio']:.0f}% | {t['avg_lb']:.1f} | {t['plate_count']} | {t['score']:.1f} | {track_badge(t)} | {'✅' if t['is_pool'] else '❌'} | {'✅' if t['is_existing_theme'] else '❌'} | {_attr_note(t)} |")
            out.append("")

        if moderate:
            out.append("### 🟡 中等信号候选主线")
            out.append("")
            out.append("| 候选主线 | 覆盖板块数 | 归属源 | 平均当日% | 平均20日% | 平均涨股比 | 平均量比 | 上榜数 | 评分 | 成色 | 已入池? | 已有主线? | 口径标注 |")
            out.append("|:--------|:--------:|:--------|:--------:|:--------:|:--------:|:--------:|:-----:|:----:|:----:|:------:|:--------:|:--------|")
            for t in moderate:
                out.append(f"| {t['theme_name']} | {len(t['sectors'])} | {_source_label(t)} | {t['avg_zdf']:+.1f}% | {fmt_pct(t['avg_zdf_d20'])} | {t['avg_zgb_ratio']:.0f}% | {t['avg_lb']:.1f} | {t['plate_count']} | {t['score']:.1f} | {track_badge(t)} | {'✅' if t['is_pool'] else '❌'} | {'✅' if t['is_existing_theme'] else '❌'} | {_attr_note(t)} |")
            out.append("")

        if weak:
            out.append("### ⚪ 弱信号候选主线")
            out.append("")
            out.append("| 候选主线 | 覆盖板块数 | 归属源 | 平均当日% | 平均20日% | 平均涨股比 | 平均量比 | 评分 | 已入池? | 已有主线? | 口径标注 |")
            out.append("|:--------|:--------:|:--------|:--------:|:--------:|:--------:|:--------:|:----:|:------:|:--------:|:--------|")
            for t in weak:
                out.append(f"| {t['theme_name']} | {len(t['sectors'])} | {_source_label(t)} | {t['avg_zdf']:+.1f}% | {fmt_pct(t['avg_zdf_d20'])} | {t['avg_zgb_ratio']:.0f}% | {t['avg_lb']:.1f} | {t['score']:.1f} | {'✅' if t['is_pool'] else '❌'} | {'✅' if t['is_existing_theme'] else '❌'} | {_attr_note(t)} |")
            out.append("")

        # 详细板块列表
        out.append("### 候选主线覆盖板块明细")
        out.append("")
        for t in outside_themes:
            out.append(f"**{t['theme_name']}**（评分{t['score']:.1f}，强度{t['strength']}）")
            out.append("")
            out.append("| 行业板块 | 当日% | 5日% | 20日% | 涨股比 | 量比 | 20日位次 | 识别来源 |")
            out.append("|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
            for s in t["sectors"]:
                src_label = {"dim_b": "维度B", "dim_a": "维度A", "mainline": "主线吸收"}.get(
                    s.get("_source"), s.get("_source", "—"))
                out.append(f"| {s['name']} | {s['zdf']:+.1f}% | {fmt_pct(s.get('zdf_d5'))} | {fmt_pct(s.get('zdf_d20'))} | {s['zgb_ratio']:.0f}% | {s['lb']:.1f} | {s['rank_d20']}/{rank_total} | {src_label} |")
            out.append("")

    # ========== 要监控追踪的主线 ==========
    out.append("---")
    out.append("")
    out.append("## 📌 建议纳入监控追踪的主线")
    out.append("")
    out.append("> 成色评级：**🟢主线**=硬门槛过且分位和达 Q_TOP，供18:45筛股；**🟡观察**=达 Q_MID，仅跟踪不入筛；**⚫过滤**=硬门槛不过（单板块=标签现象）或分位和不足 Q_MID，剔除。")
    out.append("")
    main_targets = [t for t in outside_themes
                    if t["track_level"] == "main"
                    and not t["is_pool"] and not t["is_existing_theme"]]
    if main_targets:
        out.append("以下候选主线**不在监控池、也非当日已有主线**，建议纳入监控追踪（18:45将继续做个股筛选）：")
        out.append("")
        for i, t in enumerate(main_targets, 1):
            sec_names = "、".join(s["name"] for s in t["sectors"])
            out.append(f"{i}. **{t['theme_name']}**（🟢主线，评分{t['score']:.1f}，成色{t['quality']}）：{sec_names}（平均当日{t['avg_zdf']:+.1f}%，涨股比{t['avg_zgb_ratio']:.0f}%）")
        out.append("")
    else:
        out.append("无。")
        out.append("")

    # ========== 观察线（成色不足，暂不追踪） ==========
    watch_lines = [t for t in outside_themes
                   if t["track_level"] == "watch"
                   and not t["is_pool"] and not t["is_existing_theme"]]
    if watch_lines:
        out.append("### 🟡 观察线（成色6-8分，暂不纳入监控追踪）")
        out.append("")
        out.append("| 候选主线 | 覆盖板块数 | 评分 | 成色 | 涨股比 | 20日% | 不追踪原因 |")
        out.append("|:--------|:--------:|:----:|:----:|:------:|:-----:|:--------|")
        for t in watch_lines:
            out.append(f"| {t['theme_name']} | {len(t['sectors'])} | {t['score']:.1f} | {t['quality']} | {t['avg_zgb_ratio']:.0f}% | {fmt_pct(t['avg_zdf_d20'])} | 分位和未达 Q_TOP，仅跟踪 |")
        out.append("")

    # ========== 过滤说明（硬门槛不过） ==========
    filtered_lines = [t for t in outside_themes if t["track_level"] == "filtered"]
    if filtered_lines:
        out.append("### ⚫ 过滤掉的小/无执行性主线（不追踪）")
        out.append("")
        out.append("| 候选主线 | 覆盖板块数 | 评分 | 成色 | 过滤原因 |")
        out.append("|:--------|:--------:|:----:|:----:|:--------|")
        for t in filtered_lines:
            reasons = []
            if len(t["sectors"]) < 2:
                reasons.append("单板块=标签现象")
            if t.get("quality_pct", 0.0) < Q_MID:
                reasons.append(f"分位和P{t.get('quality_pct', 0.0):.2f}<{Q_MID}")
            out.append(f"| {t['theme_name']} | {len(t['sectors'])} | {t['score']:.1f} | {t['quality']} | {'；'.join(reasons) or '无' } |")
        out.append("")

    # ========== 已归主线板块（主线优先，设计 3.5） ==========
    if absorbed_boards:
        out.append(_absorbed_section(absorbed_boards))

    return "\n".join(out)


# ================= 待回补清单（2日确认机制） =================

def build_refill_candidates(outside_themes: List[dict],
                            existing_themes: List[dict]) -> List[dict]:
    """从池外候选主线构建 sector 级待回补清单。

    只取 strong/moderate 且不在当日 industry_themes 的候选（is_existing_theme
    的候选已在该日主线中，无需回补）。每个候选板块单独一行（sector_code 唯一）。

    返回：[{candidate_theme, sector_code, sector_name, mapped_theme}, ...]
      mapped_theme 命中既有主线 → 归属名；无法映射 → None（新主线候选）
    """
    existing_names = {t["theme_name"] for t in existing_themes}
    # 既有主线 sector_code → 主线名（精确归属优先）
    existing_sector_map = {}
    for t in existing_themes:
        rs = t.get("related_sectors", "")
        try:
            data = json.loads(rs) if rs else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        for sec in data.get("sectors", []):
            if isinstance(sec, dict) and sec.get("code"):
                existing_sector_map[sec["code"]] = t["theme_name"]

    candidates = []
    seen_codes = set()
    for theme in outside_themes:
        if theme.get("strength") not in ("strong", "moderate"):
            continue
        if theme.get("is_existing_theme"):
            continue
        candidate_theme = theme["theme_name"]
        for sec in theme.get("sectors", []):
            code = sec.get("code")
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            name = sec.get("name", "")
            # 产业归属映射：精确命中既有主线 sectors；否则无映射（新主线候选）
            mapped = existing_sector_map.get(code)
            candidates.append({
                "candidate_theme": candidate_theme,
                "sector_code": code,
                "sector_name": name,
                "mapped_theme": mapped,
            })
    return candidates


def process_refill_candidates(candidates: List[dict], date_db: str) -> List[dict]:
    """upsert 待回补清单 + 状态判定，返回全表当前明细（供报告渲染）。

    幂等：同日重跑不重复累计 confirm_count（last_seen 已是今天则只同步映射）。
    """
    today = datetime.strptime(date_db, "%Y-%m-%d").date()
    rows = []
    conn = None
    try:
        import pymysql
        conn = pymysql.connect(host="127.0.0.1", port=3306, user="wish",
                               password="<DB_PASSWORD>", database="portfolio",
                               charset="utf8mb4", autocommit=False)
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            for c in candidates:
                cur.execute(
                    "SELECT first_seen, last_seen, confirm_count, status "
                    "FROM theme_refill_candidates WHERE sector_code=%s",
                    (c["sector_code"],))
                rec = cur.fetchone()
                if rec is None:
                    # 无记录 → 新增（confirm_count=1, waiting）
                    cur.execute(
                        "INSERT INTO theme_refill_candidates "
                        "(candidate_theme, sector_code, sector_name, mapped_theme, "
                        " first_seen, last_seen, confirm_count, status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,1,'waiting')",
                        (c["candidate_theme"], c["sector_code"], c["sector_name"],
                         c["mapped_theme"], today, today))
                    continue
                status = rec["status"]
                if status in ("merged", "reverted"):
                    # 已并入/已回滚，由 16:00 环节管理，本脚本只读
                    continue
                last_seen = rec["last_seen"]
                gap = (today - last_seen).days if last_seen else 0
                if gap > 10:
                    # 断档过久 → 重置为新候选
                    cur.execute(
                        "UPDATE theme_refill_candidates SET candidate_theme=%s, "
                        "sector_name=%s, mapped_theme=%s, first_seen=%s, "
                        "last_seen=%s, confirm_count=1, status='waiting' "
                        "WHERE sector_code=%s",
                        (c["candidate_theme"], c["sector_name"], c["mapped_theme"],
                         today, today, c["sector_code"]))
                elif status in ("waiting", "confirmed"):
                    if last_seen == today:
                        # 同日重跑幂等：只同步映射，不再 +1
                        cur.execute(
                            "UPDATE theme_refill_candidates SET candidate_theme=%s, "
                            "sector_name=%s, mapped_theme=%s WHERE sector_code=%s",
                            (c["candidate_theme"], c["sector_name"], c["mapped_theme"],
                             c["sector_code"]))
                    else:
                        cur.execute(
                            "UPDATE theme_refill_candidates SET candidate_theme=%s, "
                            "sector_name=%s, mapped_theme=%s, "
                            "confirm_count=confirm_count+1, last_seen=%s "
                            "WHERE sector_code=%s",
                            (c["candidate_theme"], c["sector_name"], c["mapped_theme"],
                             today, c["sector_code"]))
                else:
                    # status='dropped' 当日重新出现 → 视为新候选复活
                    cur.execute(
                        "UPDATE theme_refill_candidates SET candidate_theme=%s, "
                        "sector_name=%s, mapped_theme=%s, first_seen=%s, "
                        "last_seen=%s, confirm_count=1, status='waiting' "
                        "WHERE sector_code=%s",
                        (c["candidate_theme"], c["sector_name"], c["mapped_theme"],
                         today, today, c["sector_code"]))

            # 状态判定：confirm_count>=2 → confirmed（仅 waiting/confirmed 晋升）
            cur.execute(
                "UPDATE theme_refill_candidates SET status='confirmed' "
                "WHERE confirm_count>=2 AND status IN ('waiting','confirmed')")
            # 今日未再现且超过 7 自然日 → dropped（仅 waiting/confirmed 淘汰）
            cur.execute(
                "UPDATE theme_refill_candidates SET status='dropped' "
                "WHERE status IN ('waiting','confirmed') "
                "AND last_seen < DATE_SUB(%s, INTERVAL 7 DAY)",
                (today,))
            conn.commit()

            cur.execute(
                "SELECT sector_code, sector_name, candidate_theme, mapped_theme, "
                "first_seen, last_seen, confirm_count, status "
                "FROM theme_refill_candidates "
                "ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'waiting' THEN 1 "
                "WHEN 'dropped' THEN 2 WHEN 'merged' THEN 3 WHEN 'reverted' THEN 4 "
                "ELSE 5 END, confirm_count DESC, last_seen DESC")
            rows = cur.fetchall()
    except Exception as e:
        print(f"    ⚠️ 待回补清单更新失败: {e}", file=sys.stderr)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return rows


def render_refill_report(date_compact: str, rows: List[dict]) -> str:
    """渲染待回补清单报告 refill_candidates_YYYYMMDD.md"""
    out = []
    out.append(f"# 🔁 池外候选主线待回补清单 {date_compact}")
    out.append("")
    out.append("> 环节：18:30（outside_pool_themes 扩展，2日确认机制）")
    out.append("> 规则：strong/moderate 池外候选（非既有主线）→ sector 级入清单；连续确认≥2 → confirmed，供次日 16:00 回补")
    out.append("> 状态：waiting 待确认 / confirmed 已确认 / dropped 已淘汰 / merged 已并入 / reverted 已回滚")
    out.append("")
    out.append("| 板块代码 | 板块名称 | 候选主线 | 归属主线(mapped_theme) | 首次出现 | 最近确认 | 连续确认数 | 状态 |")
    out.append("|:--------|:--------|:--------|:----------------------|:--------:|:--------:|:----------:|:----|")
    if rows:
        for r in rows:
            mapped = r["mapped_theme"] if r["mapped_theme"] else "—（新主线）"
            first_seen = str(r["first_seen"]) if r["first_seen"] else ""
            last_seen = str(r["last_seen"]) if r["last_seen"] else ""
            out.append(
                f"| {r['sector_code']} | {r['sector_name']} | {r['candidate_theme']} | "
                f"{mapped} | {first_seen} | {last_seen} | {r['confirm_count']} | {r['status']} |")
    else:
        out.append("| — | — | — | — | — | — | — | — |")
    out.append("")
    counts = {"confirmed": 0, "waiting": 0, "dropped": 0, "merged": 0, "reverted": 0}
    for r in rows:
        if r["status"] in counts:
            counts[r["status"]] += 1
    summary = (f"> 确认进度：confirmed {counts['confirmed']} / waiting {counts['waiting']} "
               f"/ dropped {counts['dropped']}")
    if counts["merged"]:
        summary += f" / merged {counts['merged']}"
    if counts["reverted"]:
        summary += f" / reverted {counts['reverted']}"
    out.append(summary)
    return "\n".join(out)


# ================= 主流程 =================

def main():
    parser = argparse.ArgumentParser(description="池外板块主线归纳（18:30 任务1，主营推导模式）")
    parser.add_argument("date", help="YYYYMMDD 格式日期")
    parser.add_argument("--cluster-from", dest="cluster_from", required=True,
                        help="Agent 主营推理结果 JSON（pool_derivation_result_YYYYMMDD.json）")
    parser.add_argument("--no-refill", dest="no_refill", action="store_true",
                        help="跳过 refill 写库（自测用）")
    args = parser.parse_args()

    date_compact = args.date
    date_db = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    print(f"📐 Q 参数表：Q_ZGB_OUTSIDE={Q_ZGB_OUTSIDE} | Q_BREADTH_HI={Q_BREADTH_HI} | "
          f"Q_BREADTH_LO={Q_BREADTH_LO} | Q_TREND_HI={Q_TREND_HI} | Q_TREND_LO={Q_TREND_LO} | "
          f"Q_CAP_HI={Q_CAP_HI} | Q_CAP_LO={Q_CAP_LO} | Q_DEPTH_HI={Q_DEPTH_HI} | "
          f"Q_DEPTH_LO={Q_DEPTH_LO} | Q_TOP={Q_TOP} | Q_MID={Q_MID}", file=sys.stderr)
    print("📐 校准来源：分位参数，20 交易日均值校准，非市场常量", file=sys.stderr)

    # 加载数据
    print("📊 加载全A排行快照...", file=sys.stderr)
    rank_snapshot = load_latest_rank_snapshot()
    if not rank_snapshot:
        print("❌ sector_rank_daily 无数据（17:30 未落库）", file=sys.stderr)
        sys.exit(1)
    snap_date = rank_snapshot[0]["date"]
    print(f"  快照日期: {snap_date}，共 {len(rank_snapshot)} 个行业", file=sys.stderr)

    print("📋 加载监控池板块...", file=sys.stderr)
    pool_codes = load_pool_codes()
    print(f"  监控池 {len(pool_codes)} 个板块代码", file=sys.stderr)

    print("📋 加载 industry_themes...", file=sys.stderr)
    existing_themes = load_industry_themes(date_db)
    print(f"  当日 {len(existing_themes)} 条主线", file=sys.stderr)

    print("📋 加载板块统计文件...", file=sys.stderr)
    plate_file = os.path.join(PLATE_DIR, f"plate_{date_compact}.md")
    plate_t2 = parse_plate_table2(plate_file)
    concepts = parse_plate_table1_concepts(plate_file)
    print(f"  板块统计: {len(plate_t2)} 个板块, {len(concepts)} 个概念标签", file=sys.stderr)

    # 主营推导模式：消费 Agent 推理结果
    print(f"\n🧠 主营推导模式：消费推理结果 {args.cluster_from}...", file=sys.stderr)
    with open(args.cluster_from, encoding="utf-8") as f:
        result = json.load(f)
    derivation_unavailable = bool(result.get("derivation_unavailable", False))
    if derivation_unavailable:
        print("  ⚠️ 推导不可用（derivation_unavailable=true）：候选全部独立观察", file=sys.stderr)
    outside_themes, absorbed_boards = consume_derivation_result(result, rank_snapshot, pool_codes,
                                                                 plate_t2, existing_themes)

    print(f"  识别出 {len(outside_themes)} 条候选主线", file=sys.stderr)
    for t in outside_themes:
        print(f"  [{t['strength']}] {t['theme_name']}（评分{t['score']:.1f}，{len(t['sectors'])}个板块）", file=sys.stderr)
    if absorbed_boards:
        print(f"  🔄 主线优先吸收 {len(absorbed_boards)} 个板块（已属当日主线，不输出为池外候选）", file=sys.stderr)
        for b in absorbed_boards:
            div = " 🔄分歧" if b.get("derivation_divergence") else ""
            print(f"    {b.get('name','')} → {b.get('mainline_theme','')}{div}", file=sys.stderr)

    # 任务1b：池外候选主线 → 待回补清单（2日确认机制）
    if not args.no_refill:
        print("\n🔁 任务1b：池外候选主线 → 待回补清单...", file=sys.stderr)
        refill_candidates = build_refill_candidates(outside_themes, existing_themes)
        print(f"  候选板块 {len(refill_candidates)} 个（sector 级，strong/moderate 非既有主线）", file=sys.stderr)
        refill_rows = process_refill_candidates(refill_candidates, date_db)
        refill_report = render_refill_report(date_compact, refill_rows)
        refill_file = os.path.join(DATA_DIR, f"refill_candidates_{date_compact}.md")
        with open(refill_file, "w", encoding="utf-8") as f:
            f.write(refill_report)
        print(f"✅ 待回补清单: {refill_file}", file=sys.stderr)
    else:
        print("\n⏭️ --no-refill：跳过 refill 写库", file=sys.stderr)

    # 生成报告
    print("\n📝 生成报告...", file=sys.stderr)
    report = render_report(date_compact, outside_themes, rank_snapshot, pool_codes, plate_t2, concepts,
                           derivation_unavailable=derivation_unavailable,
                           absorbed_boards=absorbed_boards)

    output_file = os.path.join(DATA_DIR, f"outside_pool_themes_{date_compact}.md")
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(report)

    # 生成结构化 JSON（供 18:45 outside_pool_leaders.py 读取）
    json_file = os.path.join(DATA_DIR, f"outside_pool_themes_{date_compact}.json")
    # sectors 中的 _source/_purpose 是临时的，序列化时一并保留（18:45 需要 code/name）
    payload = {
        "date": date_compact,
        "snap_date": snap_date,
        "pool_count": len(pool_codes),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "derivation_unavailable": derivation_unavailable,
        "absorbed_boards": absorbed_boards,
        "themes": outside_themes,
    }
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 报告已生成: {output_file}", file=sys.stderr)
    print(f"✅ JSON 已生成: {json_file}", file=sys.stderr)
    print(report)


if __name__ == "__main__":
    main()
