#!/usr/bin/env python3
"""
16:30 趋势跟随任务 — 主线级趋势分析（闭环）

从 MySQL industry_themes 表读取当日产业主线（状态、天数、峰值、related_sectors），
代表股按三级降级取数：成分基线优先（theme_constituents is_current=1 股票）→
related_sectors.sectors（行业板块代码 pt0180xxx）getBoardRankList 全量成分股回退 →
表格1(概念板块成分股) concepts 映射回退。
批量获取代表股K线（150天），等权合成"主线指数K线"，
计算主线级趋势上下文（走势/阶段/幅度/天数）+ 技术确认维度（MA5/MA10/MA20 破位分级、
近5日跌幅、量比），结合主线状态机（active/peaking/fading/ended）输出主线级交易建议。
技术确认结果（break_level / advice_signal）回填当日 industry_themes 记录，
供 16:45 fetch_theme_leaders.py 作为建池准入门槛（16:30→16:45 数据通道）。

建议口径（统一按技术确认维度分级，不按状态机标签一刀切）：
1. peaking 不机械"只减不加"——按破位分级：未破位(站上MA5)→持有观察；
   跌破MA5→减仓观察；跌破MA5+MA10→才转防守。
2. fading 不一刀切"规避"——趋势未破坏(未破MA10)→降级跟踪观察；
   超跌(近5日跌幅≤-5%)→可博弈超跌反弹；破位确认才规避。
3. 状态机与 active 共用同一套技术确认维度（破位分级/量比），消除同报告内部
   "同级强度相反建议"的矛盾。
4. 板块级候选增加全市场相对强度位次（当日/20日涨幅位次、量比、涨股比）。
5. 所有建议带 [进攻]/[防守]/[中性]/[博弈]/[持有]/[谨慎] 信号语义标签。

用法：
  python3 scripts/fetch_trend.py                      # 默认最新板块文件 + 从表读主线
  python3 scripts/fetch_trend.py --date 2026-08-19    # 指定日期
  python3 scripts/fetch_trend.py --no-themes          # 跳过主线级（仅板块级）
  python3 scripts/fetch_trend.py --no-sectors         # 跳过板块级（仅主线级）
"""

import sys
import os
import re
import json
import asyncio
import argparse
import urllib.request

sys.path.insert(0, "/home/agentuser/.hermes/skills/wequant-finance")
from finance_plugin import FinancePlugin, KlineData

# 共享技术确认模块（16:30/16:45 同口径 MA 破位分级）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tech_context import compute_tech_context

try:
    import pymysql
except ImportError:
    print("需要安装 pymysql: pip install pymysql")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATE_DIR = os.path.join(BASE_DIR, "data", "plate")
MIN_LISTING = 3  # 上榜数 ≥ 3 只才算有信号（三步启动确认阈值）

# ── MySQL 配置（wish 凭据连 portfolio）──
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "wish",
    "password": "<DB_PASSWORD>",
    "database": "portfolio",
    "charset": "utf8mb4",
}

STATUS_ICON = {"active": "🟢", "peaking": "🟡", "fading": "🔴", "ended": "⚫"}
FILTER_MODE = 'OFF'   # 判层必须全量成分股（F1 收盘≥MA20 过滤仅限选股/建池层）
SCENE_GATE_ENABLED = True  # 场景门控总开关（R1/R2），异常可关


def get_db():
    conn = pymysql.connect(**DB_CONFIG)
    conn.autocommit(True)
    return conn


def get_latest_plate_file():
    """获取最新板块统计文件路径（data/plate/plate_YYYYMMDD.md）"""
    if not os.path.isdir(PLATE_DIR):
        return None
    files = []
    for f in os.listdir(PLATE_DIR):
        m = re.match(r"plate_(\d{8})\.md$", f)
        if m:
            files.append((m.group(1), os.path.join(PLATE_DIR, f)))
    if not files:
        return None
    files.sort(reverse=True)
    return files[0][1]


def parse_plate_table2(filepath):
    """解析板块统计文件表格2：二级行业板块统计，返回 {code: {name, count}}"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    table2_match = re.search(
        r"## 表格2：二级行业板块统计.*?\n(.+?)(?=\n##|\Z)", content, re.DOTALL
    )
    if not table2_match:
        return {}
    table2_text = table2_match.group(1)
    result = {}
    # 匹配表格行：| 序号 | 板块名称 | 板块代码 | 出现次数 | 股票列表 |
    pattern = r"\|\s*\d+\s*\|\s*([^|]+?)\s*\|\s*(pt\d+)\s*\|\s*(\d+)\s*\|"
    for m in re.finditer(pattern, table2_text):
        name = m.group(1).strip()
        code = m.group(2).strip()
        count = int(m.group(3))
        result[code] = {"name": name, "count": count}
    return result


def parse_plate_table1(filepath):
    """解析板块统计文件表格1：概念板块成分股，返回 concept_stocks: {概念: [code,...]}"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    table1_match = re.search(r"## 表格1.*?\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
    if not table1_match:
        return {}
    from collections import defaultdict
    concept_stocks = defaultdict(list)
    for line in table1_match.group(1).strip().split("\n"):
        if not line.startswith("|"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 9:
            continue
        code = parts[3]
        tags = [t.strip() for t in re.split(r"[、,，]", parts[8]) if t.strip()]
        for t in tags:
            concept_stocks[t].append(code)
    return concept_stocks


def load_themes_from_db(date_str):
    """从 MySQL industry_themes 表读取当日主线"""
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT theme_name, status, consecutive_days, peak_count, "
                "related_sectors, score_heat, score_fund, advice_signal "
                "FROM industry_themes WHERE theme_date = %s AND is_legacy = 0 "
                "ORDER BY peak_count DESC, id",
                (date_str,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def extract_concepts(related_sectors_json):
    """从 related_sectors JSON 提取概念标签列表"""
    try:
        sectors = json.loads(related_sectors_json)
        if isinstance(sectors, dict):
            return sectors.get("concepts", [])
    except Exception:
        pass
    return []


def extract_sectors(related_sectors_json):
    """从 related_sectors JSON 提取行业板块列表 [{"name":..., "code":...}, ...]"""
    try:
        sectors = json.loads(related_sectors_json)
        if isinstance(sectors, dict):
            return sectors.get("sectors", [])
    except Exception:
        pass
    return []


# 行业板块成分股缓存：{pt代码: [code, ...]}
_SECTOR_STOCKS_CACHE = {}


def fetch_sector_stocks(board_code):
    """按行业板块代码（pt0180xxx）拉取全量成分股代码（腾讯 getBoardRankList）。

    行业板块成分股比表格1(当日概念TOP10上榜股)覆盖更完整、稳定，
    是 sectors 回退层的代表股来源。带进程内缓存。
    """
    if board_code in _SECTOR_STOCKS_CACHE:
        return _SECTOR_STOCKS_CACHE[board_code]
    url = (
        f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
        f"?board_code={board_code}&sort_type=priceRatio&direct=down&_appver=7.3&count=200"
    )
    codes = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
        rank_list = data.get("data", {}).get("rank_list", [])
        codes = [x.get("code") for x in rank_list if x.get("code")]
    except Exception:
        codes = []
    _SECTOR_STOCKS_CACHE[board_code] = codes
    return codes


def load_theme_constituents(theme_name):
    """从 theme_constituents 表读取成分基线（P2 §4.2 指数合成源切换）。

    返回 is_current=1 的 stock_code 列表；表空/该链无成分 → 返回 []。
    不写库，只读。
    """
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code FROM theme_constituents "
                "WHERE theme_name=%s AND is_current=1",
                (theme_name,)
            )
            return [row[0] for row in cur.fetchall() if row[0]]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_theme_stocks(t, concept_stocks):
    """主线代表股（P2 §4.2）：成分基线优先 → sectors 回退降级 → concepts 回退。

    优先级：
      1. theme_constituents 表（成分基线，is_current=1 的股票集）→ source="constituents"
      2. related_sectors.sectors → getBoardRankList 全量成分股 → source="sectors"
      3. related_sectors.concepts → 表格1 → source="concepts"

    Args:
        t: theme dict（含 related_sectors JSON）
        concept_stocks: 表格1解析出的 {概念: [code,...]}（回退用）
    Returns:
        (codes, source) — source ∈ {"constituents", "sectors", "concepts"}
    """
    # 1) 成分基线优先（P2 §4.2）
    theme_name = t.get("theme_name", "")
    if theme_name:
        baseline = load_theme_constituents(theme_name)
        if baseline:
            return list(dict.fromkeys(baseline)), "constituents"

    # 2) sectors 回退（旧逻辑）
    sectors = extract_sectors(t.get("related_sectors", "{}"))
    if sectors:
        codes = []
        for s in sectors:
            code = s.get("code", "") if isinstance(s, dict) else ""
            if code:
                codes.extend(fetch_sector_stocks(code))
        codes = list(dict.fromkeys(codes))
        if codes:
            return codes, "sectors"
    # 3) concepts 回退
    concepts = extract_concepts(t.get("related_sectors", "{}"))
    codes = []
    for c in concepts:
        codes.extend(concept_stocks.get(c, []))
    return list(dict.fromkeys(codes)), "concepts"


def filter_weak_constituents(stock_klines):
    """F1 过滤：剔除 收盘 < MA20 的弱势成分股（无未来函数，用 <=报告日K线）。

    Args:
        stock_klines: {code: [KlineData, ...]}（已由调用方 truncate 到报告日）
    Returns:
        (kept, dropped): kept={code: klines}, dropped=[code,...]
    - MA20 计算：需要 >=21 根K线；不足 21 根 → 宽容保留（不误杀次新股）
    - 仅用 stock_klines 已有数据，天然无未来函数
    """
    if FILTER_MODE != 'F1':
        return stock_klines, []
    kept, dropped = {}, []
    for code, klines in stock_klines.items():
        closes = [k.close for k in klines]
        if len(closes) < 21 or closes[-1] >= sum(closes[-20:]) / 20.0:
            kept[code] = klines
        else:
            dropped.append(code)
    return kept, dropped


def build_theme_index_kline(stock_klines_map):
    """
    把主线内代表股K线按日期对齐，等权合成"主线指数K线"

    Args:
        stock_klines_map: {code: [KlineData, ...]}（各股票K线）
    Returns:
        list[KlineData] 按日期升序的主线指数K线（无效则返回 []）
    """
    if not stock_klines_map:
        return []
    by_date = {}
    for code, klines in stock_klines_map.items():
        for k in klines:
            d = k.date
            if d not in by_date:
                by_date[d] = []
            by_date[d].append(k)
    dates = sorted(by_date.keys())
    index_klines = []
    for d in dates:
        items = by_date[d]
        if not items:
            continue
        n = len(items)
        kd = KlineData()
        kd.date = d
        kd.open = sum(x.open for x in items) / n
        kd.close = sum(x.close for x in items) / n
        kd.high = sum(x.high for x in items) / n
        kd.low = sum(x.low for x in items) / n
        kd.vol = sum(x.vol for x in items) / n
        index_klines.append(kd)
    return index_klines


def compute_tech_context_from_klines(index_kline):
    """从主线指数K线（KlineData 列表）计算技术确认维度（共享模块同口径）。

    Args:
        index_kline: KlineData 列表（时间升序）
    Returns:
        dict: 同 tech_context.compute_tech_context；数据不足返回 None
    """
    if not index_kline:
        return None
    closes = [k.close for k in index_kline]
    vols = [k.vol for k in index_kline]
    return compute_tech_context(closes, vols)


def truncate_klines_to(klines_map, date_str):
    """把 K线截断到指定日期（含），用于 --date 历史回放：分析"该日"技术状态而非当前。

    KlineData.date 为 "YYYY-MM-DD" 字符串，字典序即时间序，可直接 <= 比较。
    """
    if not date_str or not klines_map:
        return klines_map
    for code in list(klines_map.keys()):
        klines_map[code] = [k for k in klines_map[code] if k.date <= date_str]
    return klines_map


def tech_txt(tech):
    """技术确认维度的可读摘要"""
    if tech is None:
        return "技术维度:N/A"
    bl = tech["break_level"]
    bl_names = {0: "未破位", 1: "破MA5", 2: "破MA5+MA10", 3: "破MA5+MA10+MA20"}
    return (
        f"技术维度:{bl_names[bl]}(MA5={tech['ma5']:.2f}/MA10={tech['ma10']:.2f}/"
        f"MA20={tech['ma20']:.2f}) 近5日:{tech['ret5']:+.1f}% 量比:{tech['vol_ratio']:.1f}"
    )


def theme_advice(status, ctx, tech, consecutive_days, peak_count, stock_count):
    """
    主线级交易建议：状态机 × 技术确认维度 → 带信号语义的建议

    规则（所有状态共用破位分级，消除状态机标签与真实趋势脱节）：
    - ended → 结束，不交易
    - fading:
        - 未破位(break_level<2) 且 近5日跌幅≤-5% → 超跌反弹可博弈
        - 未破位(break_level<2) → 退潮初期但趋势未破坏，跟踪观察
        - 破位确认(break_level>=2) → 退潮确认，规避
    - peaking:
        - 未破位(break_level=0) → 高位钝化但趋势未破，持有观察，回踩低吸不追高
        - 破MA5(break_level=1) → 高位转弱预警，减仓观察
        - 破MA5+MA10(break_level>=2) → 高位破位确认，只减不加
    - active:
        - 破位(break_level>=2) → 走弱破位，降级观望
        - 上涨/反弹 → 重点跟踪，等回踩企稳介入（幅度≥30%标注强趋势）
        - 调整 → 上涨中继，观察缩量企稳
        - 下跌 → 走弱警惕，观望
    """
    trend = ctx.get("trend_type", "趋势不明")
    days_txt = f"第{consecutive_days}天" if consecutive_days else "—"
    peak_txt = f"峰值{peak_count}只" if peak_count else "—"
    stock_txt = f"{stock_count}只代表股" if stock_count else "无代表股"
    suffix = f" | {stock_txt} {days_txt} {peak_txt}"

    # 技术数据不足（<21根K线）：退化到保守逻辑
    if tech is None:
        if status == "ended":
            return f"⚫ [防守] 主线已结束，不参与{suffix}"
        if status == "fading":
            return f"🔴 [防守] 主线退潮中，规避，不新开仓{suffix}"
        if status == "peaking":
            return f"🟡 [谨慎] 主线高位预警，只减不加，不追高（技术数据不足）{suffix}"
        if trend in ("上涨", "反弹"):
            return f"🟢 [进攻] 主线趋势向上，重点跟踪，等回踩企稳介入{suffix}"
        if trend == "调整":
            return f"🟡 [中性] 主线上涨中继调整，观察缩量企稳信号{suffix}"
        if trend == "下跌":
            return f"🔴 [防守] 主线指数走弱（{trend}），警惕退潮，观望为主{suffix}"
        return f"🟡 [中性] 主线趋势不明（{trend}），继续观察{suffix}"

    break_level = tech["break_level"]
    ret5 = tech["ret5"]
    vol_ratio = tech["vol_ratio"]

    if status == "ended":
        return f"⚫ [防守] 主线已结束，不参与{suffix}"

    if status == "fading":
        if break_level >= 2:
            return (
                f"🔴 [防守] 主线退潮确认（跌破MA5+MA10），规避，不新开仓"
                f" | {tech_txt(tech)}{suffix}"
            )
        if ret5 <= -5:
            return (
                f"🟠 [博弈] 主线退潮但已超跌（近5日{ret5:.1f}%），可轻仓博弈超跌反弹，不追高"
                f" | {tech_txt(tech)}{suffix}"
            )
        return (
            f"🟡 [观察] 主线退潮初期但趋势未破坏（未破MA10），跟踪观察，跌破MA10转规避"
            f" | {tech_txt(tech)}{suffix}"
        )

    if status == "peaking":
        if break_level >= 2:
            return (
                f"🔴 [防守] 主线高位破位确认（跌破MA5+MA10），只减不加"
                f" | {tech_txt(tech)}{suffix}"
            )
        if break_level == 1:
            return (
                f"🟠 [谨慎] 主线高位跌破MA5，减仓观察，跌破MA10转防守"
                f" | {tech_txt(tech)}{suffix}"
            )
        return (
            f"🟡 [持有] 主线高位钝化但未破位（站上MA5），持有观察，回踩MA10企稳可低吸，不追高"
            f" | {tech_txt(tech)}{suffix}"
        )

    # active —— 与 peaking/fading 共用破位分级
    if break_level >= 2:
        return (
            f"🔴 [防守] 主线走弱破位（跌破MA5+MA10），降级观望，等重新企稳"
            f" | {tech_txt(tech)}{suffix}"
        )
    if break_level == 1:
        return (
            f"🟠 [谨慎] 主线高位跌破MA5，减仓观察，跌破MA10转防守"
            f" | {tech_txt(tech)}{suffix}"
        )
    if trend in ("上涨", "反弹"):
        amp = ctx.get("amplitude", 0)
        vol_note = f" 放量(量比{vol_ratio:.1f})" if vol_ratio >= 1.5 else (
            f" 缩量(量比{vol_ratio:.1f})" if vol_ratio <= 0.7 else ""
        )
        if amp >= 30:
            return (
                f"🟢 [进攻] 主线趋势强（上涨{amp:.0f}%），重点跟踪，回踩MA10企稳可介入"
                f"{vol_note} | {tech_txt(tech)}{suffix}"
            )
        return (
            f"🟢 [进攻] 主线趋势向上，重点跟踪，等回踩企稳介入"
            f"{vol_note} | {tech_txt(tech)}{suffix}"
        )
    if trend == "调整":
        return f"🟡 [中性] 主线上涨中继调整，观察缩量企稳信号 | {tech_txt(tech)}{suffix}"
    if trend == "下跌":
        return f"🔴 [防守] 主线指数走弱（{trend}），警惕退潮，观望为主 | {tech_txt(tech)}{suffix}"
    return f"🟡 [中性] 主线趋势不明（{trend}），继续观察 | {tech_txt(tech)}{suffix}"


def extract_signal(advice: str) -> str:
    """从建议文本提取信号语义标签（如 [防守]/[进攻]/[持有]/[观察]/[博弈]/[中性]）"""
    m = re.search(r"\[([^\]]+)\]", advice)
    return m.group(1) if m else ""


def compute_three_dimension(tech, theme):
    """三维评分 + 状态修正（设计文档第三节决策树，3.1 首层基线 + 3.3 16:30 覆盖）。

    T 技术分 = bl 映射（主导）∩ 主线指数 MA60 结构（补充），取 min：
    - bl 映射: 0→3, 1→2, 2→1, 3→0（16:30 真实 break_level）
    - MA 结构（设计 3.1 L38，板块指数 MA20/MA60 + MA20 斜率）:
      ≥MA20且MA20向上=3, ≥MA20=2, <MA20但≥MA60=1, <MA60且MA20向下=0
    - T=0 当 bl≥3（破MA20） 或 破MA60且MA20向下（设计 3.1 第一层"或"关系）
    - MA60/斜率数据不足时宽容：不因缺数据判 0（不误杀 ended）

    tech: compute_tech_context 结果；theme: dict（含 status/score_heat/score_fund）。
    Returns: (new_status, score_tech)；tech 为 None 时返回 (None, None) 表示按原状态。
    """
    if tech is None:
        return None, None
    # T 技术分：bl 映射为主
    bl = tech["break_level"]
    t_tech = {0: 3, 1: 2, 2: 1, 3: 0}[bl]
    # T 补充：主线指数 MA60 结构（任一判弱则降档；数据不足宽容不误杀）
    close, ma20 = tech.get("close"), tech.get("ma20")
    ma60 = tech.get("ma60")
    ma20_slope_up = tech.get("ma20_slope_up")
    if ma20 is not None and close is not None:
        if close >= ma20:
            t_ma = 3 if ma20_slope_up is True else 2
        else:  # close < MA20
            if ma60 is None:
                t_ma = 1  # MA60 数据不足 → 临界，不判 0
            elif close >= ma60:
                t_ma = 1
            else:  # close < MA60
                t_ma = 0 if ma20_slope_up is False else 1
        t_tech = min(t_tech, t_ma)
    # H 热度分：来自 16:00 推理写入的 score_heat（16:30 已叠加全A位次修正，NULL 视为 1 中性，不误杀）
    h_heat = theme.get("score_heat")
    h_heat = h_heat if h_heat is not None else 1
    # F 基本面分：来自 16:00 推理写入的 score_fund（NULL 视为 2 中性，不误杀）
    f_fund = theme.get("score_fund")
    f_fund = f_fund if f_fund is not None else 2
    # 决策树（设计文档 3.1，优先级自上而下）
    if t_tech == 0:
        new_status = "ended"
    elif f_fund == 0:
        new_status = "ended"
    elif t_tech >= 2 and f_fund >= 2:
        new_status = "active" if h_heat >= 1 else "peaking"
    elif t_tech + f_fund >= 4:
        new_status = "peaking"
    elif t_tech == 1:
        new_status = "peaking" if h_heat >= 1 else "fading"
    else:
        new_status = "fading"
    return new_status, t_tech


def apply_scene_gate(new_status, prev_status, sh_pct):
    """场景门控（设计文档 3.2，R1/R2）。

    R1 大跌日 ended 延迟确认：报告日上证≤−0.5% 时，三维判定 ended → 降 fading 观察，
        不输出强看空（次日回踩不破再确认）。回测：避免 4 次超跌反弹日杀跌错误。
    R2 低位升级减速带：报告日 |上证|≥0.5%（普涨或大跌）时，前一交易日 fading/ended
        今日升 active → 只升 peaking（不确认强势、不追高）。回测：3/3 拦截进池即亏。

    Args:
        new_status: 三维判定的新状态（compute_three_dimension 返回）
        prev_status: 前一交易日该主线状态（industry_themes 前一日记录；无则 None）
        sh_pct: 报告日上证指数当日涨跌幅（%）；None 表示获取失败（保守不门控）

    Returns:
        (status, tag): 门控后状态 + 门控标签（None/"R1延迟确认"/"R2降速"）
    """
    if not SCENE_GATE_ENABLED or sh_pct is None or new_status is None:
        return new_status, None
    tag = None
    if sh_pct <= -0.5 and new_status == "ended":
        new_status, tag = "fading", "R1延迟确认"
    elif abs(sh_pct) >= 0.5 and prev_status in ("fading", "ended") and new_status == "active":
        new_status, tag = "peaking", "R2降速"
    return new_status, tag


def apply_zero_listing_guard(prev_status, new_status, sector_codes, table2_counts):
    """T4: 0 上榜禁反向修正。

    当三维判定将 ended/fading → active/peaking（反向修正）时，检查该主线所有
    sectors 板块在当日表格2 上榜数合计。合计=0 → 0 关注度主线，禁止反向拉回，
    维持原退潮状态。

    Args:
        prev_status: 前一交易日状态（ended/fading/active/peaking/None）
        new_status: 三维判定新状态
        sector_codes: 该主线 sectors 板块代码列表
        table2_counts: {sector_code: 上榜数}（parse_plate_table2 提取）
    Returns:
        (status, tag): tag="0上榜禁反向" 表示拦截，None 表示放行
    """
    if new_status not in ("active", "peaking"):
        return new_status, None
    if prev_status not in ("ended", "fading"):
        return new_status, None
    total = sum(table2_counts.get(c, 0) for c in sector_codes)
    if total > 0:
        return new_status, None
    return prev_status, "0上榜禁反向"


def fetch_prev_status(date_str, theme_name):
    """查询前一交易日该主线 status（R2 场景门控 prev_status 取数）。

    取 industry_themes 中 theme_date < date_str 的最大日期记录 status；
    当日运行=上一交易日，--date 回放=回放日前一日（信息边界与当日一致）。
    查询失败/无记录返回 None（保守不门控）。
    """
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM industry_themes "
                    "WHERE theme_name=%s AND theme_date<%s "
                    "ORDER BY theme_date DESC LIMIT 1",
                    (theme_name, date_str),
                )
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        print(f"⚠️ prev_status 查询失败({theme_name})，R2门控跳过: {e}", file=sys.stderr)
        return None


async def fetch_sh_index_pct(date_str=None):
    """拉取上证指数当日涨跌幅（%）。

    拉取上证指数（sh000001）日K线，取最新两根计算当日涨跌幅
    (close-prev_close)/prev_close*100。复用模块内 FinancePlugin K线请求（与全文一致）。
    失败返回 None → 门控跳过（保守不误伤），stderr 告警。

    Args:
        date_str: 报告日（YYYY-MM-DD）。当日运行传 None；--date 回放传回放日，
            取 <= date_str 的最新两根（信息边界与当日一致）。
    """
    try:
        b = FinancePlugin()
        klines_map = await b._batch_fetch_klines(["sh000001"], days=40)
        klines = klines_map.get("sh000001", [])
        if date_str:
            klines = [k for k in klines if k.date <= date_str]
        if len(klines) < 2:
            print("⚠️ 上证指数K线不足2根，场景门控跳过", file=sys.stderr)
            return None
        close, prev_close = klines[-1].close, klines[-2].close
        return (close - prev_close) / prev_close * 100
    except Exception as e:
        print(f"⚠️ 上证指数涨跌幅获取失败，场景门控跳过: {e}", file=sys.stderr)
        return None


def load_sector_rank_adjust_map(before_date: str = None):
    """读取全A位次（rank_daily），用于 H 修正。

    当日运行（before_date=None）：取最近落库日（=上一交易日）位次，符合设计 3.1
    H 修正"次日生效（不影响当日建池）"——修正数据天然滞后一个交易日。
    --date 回放（before_date=回放日）：取回放日前一交易日位次，历史重算与当时
    "仅知 T-1 数据"的信息边界一致，避免误用回放日之后落库的位次。

    Returns: {ptcode: rank_daily}；无数据/异常返回 {}
    """
    try:
        conn = get_db()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                if before_date:
                    cur.execute(
                        "SELECT code, rank_daily FROM sector_rank_daily "
                        "WHERE rank_date = (SELECT MAX(rank_date) FROM sector_rank_daily "
                        "WHERE rank_date < %s) "
                        "AND rank_daily IS NOT NULL",
                        (before_date,),
                    )
                else:
                    cur.execute(
                        "SELECT code, rank_daily FROM sector_rank_daily "
                        "WHERE rank_date = (SELECT MAX(rank_date) FROM sector_rank_daily) "
                        "AND rank_daily IS NOT NULL"
                    )
                rows = cur.fetchall()
                return {r["code"]: int(r["rank_daily"]) for r in rows}
        finally:
            conn.close()
    except Exception as e:
        print(f"⚠️ sector_rank_daily 位次读取失败(H修正跳过): {e}", file=sys.stderr)
        return {}


def adjust_heat_by_rank(theme, rank_map, valid_sector_codes=None):
    """H 热度修正（设计 3.1 L36）：主线 sectors 取最优（最小）全A位次。

    rank_daily ≤25（前20%）→ +1；>62（后50%）→ -1；其余不变。
    修正幅度 cap 在 0~3；score_heat 为 NULL 或位次缺失时不修正。
    Returns: (h_adjusted, rank_used) — rank_used 为实际使用的位次（无修正时 None）
    """
    h = theme.get("score_heat")
    if h is None or not rank_map:
        return h, None
    codes = [s.get("code", "") for s in extract_sectors(theme.get("related_sectors", "{}")) if isinstance(s, dict)]
    if valid_sector_codes is not None:
        codes = [c for c in codes if c in valid_sector_codes]
    ranks = [rank_map[c] for c in codes if c in rank_map]
    if not ranks:
        return h, None
    best_rank = min(ranks)
    h_adj = h
    if best_rank <= 25:
        h_adj = min(h + 1, 3)
    elif best_rank > 62:
        h_adj = max(h - 1, 0)
    return h_adj, best_rank


def update_theme_tech(date_str, theme_name, tech, signal, status_new=None, score_tech=None, score_heat=None):
    """回填主线技术确认到 industry_themes（16:30→16:45 数据通道）。

    break_level/advice_signal 由 16:30 计算后写入当日记录，
    16:45 fetch_theme_leaders 读取该列为建池准入门槛。
    若 status_new 非 None，则三维决策树重算后覆盖修正 status + score_tech（允许双向）。
    score_heat 非 None 时同步写回 H 热度修正值（全A位次修正，设计 3.1 L36）。
    技术数据不足（tech 为 None）时仅回填 break_level/advice_signal，不触碰 status。
    失败仅告警，不中断分析。
    """
    break_level = tech["break_level"] if tech else None
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                if status_new is not None:
                    cur.execute(
                        "UPDATE industry_themes SET break_level=%s, advice_signal=%s, "
                        "status=%s, score_tech=%s, score_heat=COALESCE(%s, score_heat) "
                        "WHERE theme_date=%s AND theme_name=%s",
                        (break_level, signal or None, status_new, score_tech, score_heat,
                         date_str, theme_name),
                    )
                else:
                    cur.execute(
                        "UPDATE industry_themes SET break_level=%s, advice_signal=%s "
                        "WHERE theme_date=%s AND theme_name=%s",
                        (break_level, signal or None, date_str, theme_name),
                    )
        finally:
            conn.close()
    except Exception as e:
        print(f"⚠️ 主线技术确认回填失败({theme_name}): {e}", file=sys.stderr)


async def fetch_sector_rank_map():
    """拉取全量二级行业排行（腾讯 getRank, hy2, count=200）。

    Returns:
        {ptcode: {name, zdf, zdf_d5, zdf_d20, zgb, lb}}；失败返回 {}
    """
    url = (
        "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
        "?board_type=hy2&sort_type=PriceRatioD20&direct=down"
        "&selector=not_in_market%3Dgn_fg&count=200"
    )
    result = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
        rank_list = data.get("data", {}).get("rank_list", [])
        for item in rank_list:
            code = item.get("code", "")
            if not code:
                continue
            result[code] = {
                "name": item.get("name", ""),
                "zdf": float(item.get("zdf", 0) or 0),
                "zdf_d5": float(item.get("zdf_d5", 0) or 0),
                "zdf_d20": float(item.get("zdf_d20", 0) or 0),
                "zgb": item.get("zgb", ""),
                "lb": float(item.get("lb", 0) or 0),
            }
    except Exception:
        result = {}
    return result


def sector_rank_txt(code, name, rank_map):
    """板块全市场相对强度位次文本（当日/20日涨幅位次 + 量比 + 涨股比）"""
    if not rank_map:
        return ""
    all_codes = list(rank_map.keys())
    if code not in rank_map or not all_codes:
        return ""
    total = len(all_codes)
    zdf_sorted = sorted(all_codes, key=lambda c: rank_map[c]["zdf"], reverse=True)
    d20_sorted = sorted(all_codes, key=lambda c: rank_map[c]["zdf_d20"], reverse=True)
    zdf_rank = zdf_sorted.index(code) + 1
    d20_rank = d20_sorted.index(code) + 1
    info = rank_map[code]
    return (
        f" | 全A位次:当日{zdf_rank}/{total}(前{zdf_rank/total*100:.0f}%)"
        f" 20日{d20_rank}/{total}(前{d20_rank/total*100:.0f}%)"
        f" 量比{info['lb']:.1f} 涨股比{info['zgb']}"
    )


async def analyze_themes(args, label):
    """主线级分析：从表读主线 → 概念映射代表股 → 合成指数 → 趋势+技术确认 → 建议"""
    date_str = label[:4] + "-" + label[4:6] + "-" + label[6:8]
    themes = load_themes_from_db(date_str)
    if not themes:
        print(f"ℹ️ {date_str} industry_themes 表无主线记录，跳过主线级分析")
        return

    print(f"🏭 产业主线级分析（{date_str}，来自 industry_themes 表，共 {len(themes)} 条）\n")

    # 表格1概念→股票映射（回退：无 sectors 字段时使用）
    plate_file = os.path.join(PLATE_DIR, f"plate_{label}.md")
    concept_stocks = parse_plate_table1(plate_file)

    # T4: 当日表格2 上榜数（0上榜禁反向防线，循环外解析一次）
    table2_data = parse_plate_table2(plate_file)
    table2_counts = {code: info["count"] for code, info in table2_data.items()}

    # H 热度修正位次表（sector_rank_daily 最近落库=上一交易日，设计 3.1 L36 次日生效）
    rank_adjust_map = load_sector_rank_adjust_map(args.date if args.date else None)

    # 收集所有主线代表股（优先行业板块 sectors → getBoardRankList 全量成分股）
    theme_stocks_map = {}  # theme_name -> [code, ...]
    for t in themes:
        codes, source = get_theme_stocks(t, concept_stocks)
        theme_stocks_map[t["theme_name"]] = codes

    # 批量获取所有代表股K线（一次拉完，避免多次请求）
    all_codes = []
    for codes in theme_stocks_map.values():
        all_codes.extend(codes)
    all_codes = list(dict.fromkeys(all_codes))

    if all_codes:
        try:
            b = FinancePlugin()
            klines_map = await b._batch_fetch_klines(all_codes, days=150)
            # --date 回放：截断到目标日，分析"该日"技术状态
            truncate_klines_to(klines_map, date_str)
        except Exception as e:
            print(f"❌ K线批量获取失败: {e}")
            klines_map = {}
    else:
        klines_map = {}

    # 场景门控：上证当日涨跌幅（循环外取一次，失败 None 保守不门控）
    sh_pct = await fetch_sh_index_pct(date_str)
    if sh_pct is None:
        print("   场景门控: 上证涨跌幅获取失败，门控整体跳过（保守）")
    else:
        print(f"   场景门控: 上证当日涨跌幅 {sh_pct:+.2f}%")

    failed = []
    for t in themes:
        name = t["theme_name"]
        status = t["status"]
        codes = theme_stocks_map.get(name, [])
        sector_codes = [s.get("code", "") for s in extract_sectors(t.get("related_sectors", "{}")) if isinstance(s, dict)]
        source_labels = {"constituents": "成分基线", "sectors": f"行业板块{len(sector_codes)}个", "concepts": "概念(表格1)"}
        source_txt = source_labels.get(source, f"行业板块{len(sector_codes)}个" if sector_codes else "概念(表格1)")
        stock_klines = {c: klines_map.get(c, []) for c in codes if klines_map.get(c)}
        kept_klines, dropped_codes = filter_weak_constituents(stock_klines)
        index_kline = build_theme_index_kline(kept_klines)

        icon = STATUS_ICON.get(status, "❓")
        if not index_kline or len(index_kline) < 2:
            failed.append(f"{name}(无K线数据)")
            print(
                f"{icon} {name}: {status} | 代表股{len(codes)}只/可用{len(stock_klines)}只"
                f"({source_txt}) | F1过滤{len(dropped_codes)}只/保留{len(kept_klines)}只 | ❌ 主线指数K线不足"
            )
            continue

        try:
            b = FinancePlugin()
            ctx = b._calculate_trend_context(index_kline, recent_days=20)
            tech = compute_tech_context_from_klines(index_kline)
            # 三维判定（16:30 回填真实 break_level 后重算，覆盖修正 status，允许双向）
            # H 修正：sector_rank_daily 全A位次（最优板块位次，T-1 落库次日生效）
            # T3: H修正前过滤残留板块（成分与该链 theme_constituents 无交集 → 不参与位次取数）
            constituents_baseline = set(load_theme_constituents(name))
            if constituents_baseline and sector_codes:
                valid_sector_codes = set()
                for sc in sector_codes:
                    sec_stocks = set(fetch_sector_stocks(sc))
                    if sec_stocks & constituents_baseline:
                        valid_sector_codes.add(sc)
                if len(valid_sector_codes) < len(sector_codes):
                    n_skip = len(sector_codes) - len(valid_sector_codes)
                    print(f"      └─ T3过滤: H修正跳过{n_skip}个残留板块（成分与基线无交集）")
            else:
                valid_sector_codes = None
            # H 修正幂等：仅首次 16:30 处理（advice_signal 为 NULL，16:00 不写该列）时应用；
            # 重跑时沿用已修正 score_heat，避免对已修正值重复叠加 ±1
            if t.get("advice_signal") is None:
                h_adjusted, rank_used = adjust_heat_by_rank(t, rank_adjust_map, valid_sector_codes)
                if h_adjusted is not None and h_adjusted != t.get("score_heat"):
                    t["score_heat"] = h_adjusted
                    print(f"      └─ H修正: score_heat→{h_adjusted} (全A位次{rank_used})")
            else:
                h_adjusted = None
            new_status, score_tech = compute_three_dimension(tech, t)
            # 场景门控（R1/R2，设计文档 3.2/3.3）：三维判定后、回填前
            prev_status = fetch_prev_status(date_str, name)
            # T4: 0上榜禁反向（ended/fading → active/peaking 且表格2 无该主线板块上榜 → 维持）
            guard_status, guard_tag = apply_zero_listing_guard(
                prev_status, new_status, sector_codes, table2_counts
            )
            if guard_tag:
                new_status = guard_status
                print(f"      └─ 🛑 0上榜禁反向: {prev_status} 维持（表格2 无该主线板块上榜）")
            pre_gate_status = new_status
            new_status, gate_tag = apply_scene_gate(new_status, prev_status, sh_pct)
            if gate_tag:
                market_label = "大跌日" if sh_pct <= -0.5 else "普涨日"
                print(f"      └─ 场景门控: {pre_gate_status}→{new_status} ({gate_tag} | {market_label} {sh_pct:+.2f}%)")
            # 建议以门控后终态生成（status 以三维判定+场景门控后最终值为准）
            final_status = new_status if new_status is not None else status
            advice = theme_advice(
                final_status, ctx, tech, t.get("consecutive_days") or 0,
                t.get("peak_count") or 0, len(codes)
            )
            # 技术确认回填（16:30→16:45 数据通道），status/advice_signal 均传门控后终态
            update_theme_tech(
                date_str, name, tech, extract_signal(advice), new_status, score_tech, h_adjusted
            )
            tech_disp = f" | {tech_txt(tech)}" if tech else ""
            print(
                f"{icon} {name}: {status} | 代表股{len(codes)}只({source_txt})"
                f" | F1过滤{len(dropped_codes)}只/保留{len(kept_klines)}只"
                f" | 指数走势={ctx['trend_type']}"
                f" | 阶段={ctx['current_phase']} | 幅度={ctx['amplitude']:.1f}%"
                f" | 天数={ctx['days']}{tech_disp}"
            )
            print(f"      └─ 建议: {advice}")
            if new_status is not None and new_status != status:
                print(f"      └─ ⚠️ 16:30 三维修正: {status}→{new_status}")
        except Exception as e:
            failed.append(f"{name}(趋势计算: {e})")
            print(f"{icon} {name}: {status} | ❌ 主线趋势计算失败: {e}")

    if failed:
        print(f"\n⚠️ 部分主线数据异常（{len(failed)}条）: {', '.join(failed)}")
    print()


async def analyze_sectors(args, label):
    """板块级趋势分析：候选板块 + 全市场相对强度位次"""
    filepath = os.path.join(PLATE_DIR, f"plate_{label}.md")
    sectors = parse_plate_table2(filepath)
    if not sectors:
        print(f"ℹ️ {label} 表格2为空，跳过板块级分析")
        return

    candidates = [
        (code, info["name"], info["count"])
        for code, info in sectors.items()
        if info["count"] >= args.min_count
    ]
    candidates.sort(key=lambda x: -x[2])

    if not candidates:
        print(f"ℹ️ {label} 表格2中没有上榜数 ≥ {args.min_count} 的板块，跳过板块级分析")
        return

    print(f"📊 板块级趋势跟随候选（{label}，共 {len(candidates)} 个，上榜数≥{args.min_count}）\n")

    # 全市场行业排行（相对强度位次用），失败不影响主流程
    rank_map = await fetch_sector_rank_map()

    b = FinancePlugin()
    codes = [c[0] for c in candidates]
    try:
        klines = await b._batch_fetch_klines(codes, days=150)
        # --date 回放：截断到目标日
        label_date = label[:4] + "-" + label[4:6] + "-" + label[6:8]
        truncate_klines_to(klines, label_date)
    except Exception as e:
        print(f"❌ K线批量获取失败: {e}")
        klines = {}

    failed = []
    for code, name, count in candidates:
        kl = klines.get(code, [])
        if not kl:
            failed.append(f"{name}(无K线数据)")
            print(f"{name:10s} ({code}): 上榜数={count:2d} | ❌ 无K线数据")
            continue
        try:
            ctx = b._calculate_trend_context(kl, recent_days=20)
            rtxt = sector_rank_txt(code, name, rank_map)
            print(
                f"{name:10s} ({code}): 上榜数={count:2d} | 走势={ctx['trend_type']:6s}"
                f" | 阶段={ctx['current_phase']} | 幅度={ctx['amplitude']:.1f}%"
                f" | 天数={ctx['days']}{rtxt}"
            )
        except Exception as e:
            failed.append(f"{name}(趋势计算错误)")
            print(f"{name:10s} ({code}): 上榜数={count:2d} | ❌ 趋势计算失败: {e}")

    if failed:
        print(f"\n⚠️ 部分板块数据异常（{len(failed)}个）: {', '.join(failed)}")
    print()


async def main():
    parser = argparse.ArgumentParser(description="主线级+板块级趋势分析（16:30趋势跟随）")
    parser.add_argument("--date", help="日期 YYYY-MM-DD（默认最新板块文件）")
    parser.add_argument(
        "--min-count", type=int, default=MIN_LISTING,
        help=f"板块上榜数下限（默认 {MIN_LISTING}）",
    )
    parser.add_argument("--no-themes", action="store_true", help="跳过主线级分析")
    parser.add_argument("--no-sectors", action="store_true", help="跳过板块级分析")
    args = parser.parse_args()

    if args.no_themes and args.no_sectors:
        print("⚠️ --no-themes 和 --no-sectors 同时指定，无事可做")
        sys.exit(1)

    # 确定板块文件
    if args.date:
        date_str = args.date.replace("-", "")
        filepath = os.path.join(PLATE_DIR, f"plate_{date_str}.md")
    else:
        filepath = get_latest_plate_file()
    if not filepath or not os.path.exists(filepath):
        print(f"❌ 未找到板块统计文件（{PLATE_DIR}/plate_YYYYMMDD.md）")
        sys.exit(1)
    label = os.path.basename(filepath).replace("plate_", "").replace(".md", "")

    # 主线级优先（闭环主输出）
    if not args.no_themes:
        await analyze_themes(args, label)
    # 板块级（补充输出）
    if not args.no_sectors:
        await analyze_sectors(args, label)

    print(f"✅ 趋势跟随分析完成（{label}）")


if __name__ == "__main__":
    asyncio.run(main())
