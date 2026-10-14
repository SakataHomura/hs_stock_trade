#!/usr/bin/env python3
"""
fetch_theme_leaders.py — 主线维度龙头分析（16:45）
================================================================
板块和上榜股票只作为**股票池来源**，输出按**主线维度**聚类：
对每条 active/peaking 且 16:30 技术确认未破位（break_level<2）的主线（industry_themes 表），
  1. 股票池来源A：主线覆盖板块（related_sectors.sectors）大市值成分股 top_n（腾讯 getBoardRankList 按市值排序）
  2. 股票池来源B：表格1概念板块上榜股中，概念命中主线 concepts 的股票（概念股直接来自表格1）——
     归属校验：命中股须属于主线 sectors 任一板块成分股，才并入主池参与板块内龙头判定；
     否则（如化工股借"军工/航母"概念上榜，主业非主线板块成分）标记为概念来源，
     单独分组只做跟踪观察，不参与 RPS/场景/精筛/alpha 计算，报告单独小节披露。
  合并去重后做均线照妖镜验证，输出按主线聚类：龙头/入场机会/剔除。
被 16:30 技术确认拦截的主线（break_level>=2）不建池，单独输出「🔻 技术破位降级」段落；
16:30 断档（break_level 为 NULL）按未破位保守处理，报告头标注「⚠️ 无 16:30 技术确认」。

用法：
    python3 fetch_theme_leaders.py <YYYYMMDD> [--top-n 10] [--min-ltsz 300]

依赖：
    fetch_sector_leaders.py（同目录，复用 get_all_constituents/get_stock_kline/calc_ma/classify_ma）
    tech_context.py（同目录，共享 MA 破位分级，与 16:30 同口径）
    MySQL portfolio.industry_themes（wish 凭据）
"""

import asyncio
import json
import os
import re
import sys
import pymysql
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ---- 复用 fetch_sector_leaders.py 的核心函数（均线照妖镜） ----
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_sector_leaders as fsl  # get_all_constituents, get_stock_kline, calc_rps, calc_ma, classify_ma
from tech_context import compute_tech_context  # 共享 MA 破位分级（与 16:30 同口径）

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATE_DIR = os.path.join(BASE_DIR, "data", "plate")

# F2 §三：成分基线优先建池开关（默认 True；False 回退旧逻辑：A2 板块为主 + all_sectors_skipped 回退）
POOL_CONSTITUENT_FIRST = True


# ================= 数据源 =================

def get_db():
    return pymysql.connect(
        host="127.0.0.1", port=3306, user="wish", password="<DB_PASSWORD>",
        database="portfolio", charset="utf8mb4",
    )


def load_themes_from_db(date_str: str) -> List[dict]:
    """读取当日主线（active/fading/peaking/ended）"""
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT theme_name, status, consecutive_days, peak_count, "
                "related_sectors, break_level, advice_signal, related_chains FROM industry_themes "
                "WHERE theme_date = %s AND is_legacy = 0 "
                "ORDER BY peak_count DESC, id",
                (date_str,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def load_multichain_stock_set() -> set:
    """读取跨链重复股票集（P2 §4.2④）：theme_constituents 中 is_current=1 且
    同一 stock_code 出现在 >1 个 theme_name 行 → 多链共振候选。

    返回 stock_code 集合，用于建池打标 🔄 多链共振。
    """
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, COUNT(DISTINCT theme_name) AS n "
                "FROM theme_constituents WHERE is_current=1 "
                "GROUP BY stock_code HAVING n > 1"
            )
            return {row[0] for row in cur.fetchall() if row[0]}
    except Exception:
        return set()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_theme_constituents(theme_name: str) -> set:
    """读取该链 theme_constituents（is_current=1）stock_code 集合（P4 成分基线）。

    用于建池时 sectors 残留板块过滤（T3 消费侧防线）。
    返回 stock_code 集合；表空/该链无成分/查询失败 → 返回空集。
    """
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code FROM theme_constituents "
                "WHERE theme_name=%s AND is_current=1",
                (theme_name,)
            )
            return {row[0] for row in cur.fetchall() if row[0]}
    except Exception:
        return set()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def parse_related(related_sectors_json: str) -> Tuple[List[str], List[dict]]:
    """从 related_sectors JSON 提取 (concepts, sectors)"""
    try:
        d = json.loads(related_sectors_json)
        if isinstance(d, dict):
            return d.get("concepts", []), d.get("sectors", [])
    except Exception:
        pass
    return [], []


def parse_plate_table1_full(filepath: str) -> Dict[str, List[dict]]:
    """解析表格1概念股，返回 {概念: [ {code,name,price,zdf,zdf5,zdf20}, ... ]}"""
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"## 表格1.*?\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
    if not m:
        return {}
    result = defaultdict(list)
    for line in m.group(1).strip().split("\n"):
        if not line.startswith("|"):
            continue
        parts = [p.strip() for p in line.split("|")]
        # | # | 股票名称 | 股票代码 | 最新价 | 当日涨跌幅 | 5日涨跌幅 | 20日涨跌幅 | 所属概念板块 |
        if len(parts) < 9:
            continue
        def _pct(s):
            try:
                return float(s.replace("+", "").replace("%", "").replace("--", "0"))
            except ValueError:
                return 0.0
        stock = {
            "code": parts[3],
            "name": parts[2],
            "price": _pct(parts[4]),
            "zdf": _pct(parts[5]),
            "zdf5": _pct(parts[6]),
            "zdf20": _pct(parts[7]),
            "concepts": parts[8],
        }
        for tag in re.split(r"[、,，]", parts[8]):
            tag = tag.strip()
            if tag:
                result[tag].append(stock)
    return dict(result)


async def enrich_ltsz(pool_stocks: List[dict], batch: int = 60):
    """为股票池中缺少流通市值（表格1上榜股）的股票批量补市值（腾讯 qt.gtimg.cn，字段44=流通市值亿）"""
    import urllib.request
    missing = [s for s in pool_stocks if (s.get("ltsz") or 0) <= 0]
    if not missing:
        return
    for i in range(0, len(missing), batch):
        chunk = missing[i:i + batch]
        q = ",".join(s["code"] for s in chunk)
        url = f"https://qt.gtimg.cn/q={q}"
        try:
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(url, timeout=10).read().decode("gbk", errors="ignore"))
            for line in raw.split("\n"):
                line = line.strip()
                if "=" not in line or not line.startswith("v_"):
                    continue
                payload = line.split("=", 1)[1].strip().strip('"')
                fields = payload.split("~")
                if len(fields) > 45 and fields[2]:
                    code = f"sh{fields[2]}" if line.startswith("v_sh") else f"sz{fields[2]}"
                    try:
                        ltsz = float(fields[44])
                    except ValueError:
                        continue
                    for s in pool_stocks:
                        if s["code"] == code:
                            s["ltsz"] = ltsz
                            break
        except Exception as e:
            print(f"  ⚠️ 批量补市值失败: {e}", file=sys.stderr)


async def _fetch_constituent_quotes(codes: set, batch: int = 60) -> Dict[str, dict]:
    """来源A：成分基线股批量补行情（qt.gtimg.cn），返回 {code: {name,zxj,zdf,turnover,hsl,pe,ltsz}}。

    字段实测：name[1]、zxj[3]、zdf[32]、turnover[37]、hsl[38]、pe_ttm[39]、ltsz[44]。
    """
    result: Dict[str, dict] = {}
    if not codes:
        return result
    import urllib.request
    code_list = sorted(codes)
    for i in range(0, len(code_list), batch):
        chunk = code_list[i:i + batch]
        q = ",".join(chunk)
        url = f"https://qt.gtimg.cn/q={q}"
        try:
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(url, timeout=10).read().decode("gbk", errors="ignore"))
            for line in raw.split("\n"):
                line = line.strip()
                if "=" not in line or not line.startswith("v_"):
                    continue
                payload = line.split("=", 1)[1].strip().strip('"')
                fields = payload.split("~")
                if len(fields) <= 45 or not fields[2]:
                    continue
                code = f"sh{fields[2]}" if line.startswith("v_sh") else f"sz{fields[2]}"
                def _flt(v):
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        return 0.0
                result[code] = {
                    "name": fields[1],
                    "zxj": _flt(fields[3]),
                    "zdf": _flt(fields[32]),
                    "turnover": _flt(fields[37]),
                    "hsl": _flt(fields[38]),
                    "pe": fields[39] if fields[39] else "N/A",
                    "ltsz": _flt(fields[44]),
                }
        except Exception as e:
            print(f"  ⚠️ 成分基线批量补行情失败: {e}", file=sys.stderr)
    return result


# ================= 核心分析 =================

async def _sector_tech_scan(sectors: List) -> Tuple[List[dict], Dict[str, dict]]:
    """扫描主线覆盖板块的指数技术状态（与16:30同一套MA破位分级）。

    Args:
        sectors: related_sectors.sectors（字符串列表或字典列表）
    Returns:
        (板块信息列表, {板块代码: tech_dict})
        tech_dict: {close, ma5, ma10, ma20, break_level, ret5, vol_ratio, ma20_slope, name}
        K线拉取失败/不足21根 → 该板块不进 tech_map（调用方保守放行，不误杀）
    """
    items = []
    for sec in sectors:
        if isinstance(sec, str):
            m = re.match(r'(.+)\(([a-z]+\d+),', sec)
            if m:
                code, name = m.group(2), m.group(1)
            else:
                continue
        else:
            code, name = sec.get("code", ""), sec.get("name", "")
        if not code:
            continue
        items.append({"code": code, "name": name})
    if not items:
        return items, {}

    closes_list = await asyncio.gather(*[fsl.get_stock_kline(it["code"], limit=60) for it in items])
    tech_map = {}
    for it, closes in zip(items, closes_list):
        if not closes or len(closes) < 21:
            print(f"  ⚠️ 板块 {it['name']}({it['code']}) K线拉取失败/不足，跳过强度检查（保守放行）", file=sys.stderr)
            continue
        tech = compute_tech_context(closes)
        if tech is None:
            continue
        # MA20斜率（当前MA20 相对 20日前MA20 变化率，与个股口径一致）
        ma20_slope = None
        if len(closes) >= 40:
            ma20_prev = fsl.calc_ma(closes[:-20], 20)
            if ma20_prev:
                ma20_slope = (tech["ma20"] / ma20_prev - 1) * 100
        tech["ma20_slope"] = ma20_slope
        tech["name"] = it["name"]
        tech_map[it["code"]] = tech
    return items, tech_map


async def build_pool(theme: dict, table1: Dict[str, List[dict]], top_n: int, min_ltsz: float):
    """构建主线股票池：来源A 板块大市值成分股 + 来源B 表格1概念上榜股。

    板块强度二次拦截：板块指数 break_level>=2 → 剔除该板块成分股（不建池）；
        break_level==1 或 MA20斜率<=0 → 降权（src 标 ⚠️弱板块，名额减半）
    板块强度加权排序：按板块5日涨幅(ret5) 降序分配 top_n 名额（线性加权，总量保持），弱板块压缩
    来源B（表格1概念命中）归属校验：命中股属于主线 sectors 任一板块成分股 → 并入主池参与判定；
        否则（概念标签伪影风险股）单独分组 concept_watch 只跟踪，不参与主池对比/精筛/alpha 计算。

    Returns:
        (pool_stocks, concept_watch, concepts, sectors, sector_meta)
        pool_stocks: 主池（来源A + 归属校验通过的来源B），参与龙头判定
        concept_watch: 概念来源跟踪分组（非板块成分股，仅观察）
    """
    concepts, sectors = parse_related(theme["related_sectors"])
    pool: Dict[str, dict] = {}
    concept_watch: List[dict] = []
    sector_meta = {"removed": [], "weak": [], "no_data": [], "t3_skipped": []}
    sector_stock_codes: set = set()  # 主线覆盖板块全量成分股代码集（来源B 归属校验基准）
    # T3: 成分基线（残留板块过滤，防概念换血后 sectors 静态继承污染）
    _theme_name = theme.get("theme_name", "")
    constituents_baseline = set(load_theme_constituents(_theme_name)) if _theme_name else set()

    # ---- 来源A（权威）：成分基线股（theme_constituents primary，F2 §二）----
    # 批量补行情（qt.gtimg.cn）入池，src 标 成分[基线]，min_ltsz 过滤与来源 A2/B 一致
    constituent_quotes: Dict[str, dict] = {}
    if POOL_CONSTITUENT_FIRST and constituents_baseline:
        constituent_quotes = await _fetch_constituent_quotes(constituents_baseline)
        for code, q in constituent_quotes.items():
            if q["ltsz"] >= min_ltsz:
                pool[code] = {
                    "code": code, "name": q["name"],
                    "ltsz": q["ltsz"], "zdf": q["zdf"], "zxj": q["zxj"],
                    "turnover": q["turnover"], "hsl": q["hsl"], "pe": q["pe"],
                    "src": "成分[基线]",
                    "zdf5": None, "zdf20": None,
                }

    # 板块指数技术扫描（并发拉K线）
    sec_items, tech_map = await _sector_tech_scan(sectors)

    # 按板块强度排序：ret5 降序；无K线数据排最后（保守放行，不误杀）
    def _strength_key(it):
        tech = tech_map.get(it["code"])
        return tech["ret5"] if tech and tech.get("ret5") is not None else -999.0

    sec_items.sort(key=_strength_key, reverse=True)

    # 按排名线性加权分配名额（第1名≈2倍top_n，末名≈0；总和=板块数*top_n 保持总量）
    k = len(sec_items)
    slots: Dict[str, int] = {}
    for i, it in enumerate(sec_items):
        code = it["code"]
        tech = tech_map.get(code)
        if tech is None:
            slots[code] = top_n  # 强度未知 → 默认名额（保守放行）
            sector_meta["no_data"].append(it["name"])
            continue
        break_level = tech.get("break_level") or 0
        if break_level >= 2:
            slots[code] = 0  # 剔除该板块成分股
            sector_meta["removed"].append((it["name"], code, break_level, tech.get("ret5")))
            continue
        # 线性加权：平均权重 (k+1)/2，第1名权重 k，末名权重 1 → 名额=top_n*2*rank_w/(k+1)
        rank_w = max(1, k - i)
        n = max(1, round(top_n * 2 * rank_w / (k + 1)))
        if break_level == 1 or (tech.get("ma20_slope") is not None and tech["ma20_slope"] <= 0):
            n = max(1, n // 2)  # 降权：名额减半
            sector_meta["weak"].append(it["name"])
        slots[code] = n

    # ---- 来源A：按分配名额取板块成分股（板块内按流通市值排序） ----
    sectors_with_stocks = 0
    sectors_skipped_t3 = 0
    for sec in sec_items:
        code, name = sec["code"], sec["name"]
        n_slots = slots.get(code, 0)
        if n_slots <= 0:
            continue
        stocks = await fsl.get_all_constituents(code)
        if not stocks:
            continue
        sectors_with_stocks += 1
        # T3: 残留板块过滤（成分与该链 theme_constituents 交集=0 → 跳过，防概念换血后污染）
        if constituents_baseline:
            sec_codes = {s.get("code", "") for s in stocks if s.get("code")}
            if not (sec_codes & constituents_baseline):
                print(f"  \u23ed\ufe0f 跳过残留板块 {name}（成分与该链无交集）", file=sys.stderr)
                sector_meta["t3_skipped"].append(name)
                sectors_skipped_t3 += 1
                continue
        # 记录板块全量成分股代码（来源B 归属校验基准：概念命中股是否为该主线任一板块成分股）
        sector_stock_codes.update(s.get("code", "") for s in stocks if s.get("code"))
        src_tag = f"板块[{name}]"
        if name in sector_meta["weak"]:
            src_tag += "⚠️弱板块"
        ranked = []
        for s in stocks:
            c, nm = s.get("code", ""), s.get("name", "")
            if not c or not nm:
                continue
            ranked.append({
                "code": c, "name": nm,
                "ltsz": float(s.get("ltsz", 0)),
                "zdf": float(s.get("zdf", 0)),
                "zxj": float(s.get("zxj", 0)),
                "turnover": float(s.get("turnover", 0)),
                "hsl": float(s.get("hsl", 0)),
                "pe": s.get("pe_ttm", "N/A"),
                "src": src_tag,
                "zdf5": None, "zdf20": None,
            })
        ranked.sort(key=lambda x: x["ltsz"], reverse=True)
        for st in ranked[:n_slots]:
            if st["ltsz"] >= min_ltsz:
                # 成分基线股优先保留其 src 标签（来源A 权威，不被子板块覆盖）
                if st["code"] in pool and str(pool[st["code"]].get("src", "")).startswith("成分"):
                    continue
                pool[st["code"]] = st

    # 回退链收紧（F2 §二）：仅当成分基线为空 且 全部 sectors 被过滤/破位 时才回退 concepts；
    # 成分基线非空时禁止 concepts fallback 灌池（防「全板块被过滤→概念股灌池」污染）
    all_sectors_skipped = sectors_with_stocks > 0 and sectors_skipped_t3 == sectors_with_stocks
    all_sectors_blocked = not any(slots.get(sec["code"], 0) > 0 for sec in sec_items)
    if POOL_CONSTITUENT_FIRST:
        concepts_fallback = (not constituents_baseline) and (all_sectors_skipped or all_sectors_blocked)
    else:
        concepts_fallback = all_sectors_skipped
    if concepts_fallback:
        print(f"  \U0001f504 成分基线为空且全部板块被过滤/破位，回退 concepts 建池", file=sys.stderr)

    # ---- 来源B：表格1 概念命中主线 concepts 的上榜股 ----
    # 归属校验：命中股属于主线 sectors 任一板块成分股 → 并入主池参与板块内对比/精筛；
    # 否则 → concept_watch 单独分组只跟踪（概念标签伪影风险股，如化工股借"军工/航母"概念上榜），
    #          不参与 RPS/场景/精筛/alpha 计算。
    # T3 回退：全部板块被T3过滤时 bypass 归属校验，concept 命中股直接入主池
    for concept in concepts:
        for st in table1.get(concept, []):
            code = st["code"]
            if code in pool:
                # 补充表格1的5日/20日涨幅信息
                pool[code]["zdf5"] = st["zdf5"]
                pool[code]["zdf20"] = st["zdf20"]
            elif code in sector_stock_codes or (POOL_CONSTITUENT_FIRST and code in constituents_baseline) or concepts_fallback:
                # 归属校验通过：是主板块成分股（来源A名额未覆盖但概念命中补入），参与判定
                pool[code] = {
                    "code": code, "name": st["name"],
                    "ltsz": 0.0, "zdf": st["zdf"],
                    "zxj": st["price"], "turnover": 0, "hsl": 0, "pe": "N/A",
                    "src": f"板块[归属]+上榜[{concept}]",
                    "zdf5": st["zdf5"], "zdf20": st["zdf20"],
                }
            else:
                # 归属校验失败：非主线板块成分股，纯概念来源 → 单独分组跟踪
                concept_watch.append({
                    "code": code, "name": st["name"],
                    "ltsz": 0.0, "zdf": st["zdf"],
                    "zxj": st["price"], "turnover": 0, "hsl": 0, "pe": "N/A",
                    "src": f"概念[{concept}]",
                    "zdf5": st["zdf5"], "zdf20": st["zdf20"],
                })
    # F3 §二：空池口径披露用的成分基线 Top3（按流通市值降序）
    baseline_top3 = sorted(
        [{"code": c, "name": q["name"], "ltsz": q["ltsz"]} for c, q in constituent_quotes.items()],
        key=lambda x: x["ltsz"], reverse=True)[:3]
    pool_meta = {"baseline_count": len(constituents_baseline), "baseline_top3": baseline_top3}
    return list(pool.values()), concept_watch, concepts, sectors, sector_meta, pool_meta


async def analyze_pool(pool_stocks: List[dict]) -> List[dict]:
    """对股票池逐只做 RPS + 均线结构验证（复用 fetch_sector_leaders.classify_ma）"""
    # 1) 先算全部池内个股的20日涨幅（RPS 排名基准）
    all_d20 = []
    kline_cache: Dict[str, List[float]] = {}
    for i, st in enumerate(pool_stocks):
        close_prices = await fsl.get_stock_kline(st["code"], limit=260)
        if close_prices:
            kline_cache[st["code"]] = close_prices
            if len(close_prices) >= 20:
                d20 = (close_prices[-1] - close_prices[-20]) / close_prices[-20] * 100
                all_d20.append((st["code"], d20))
        if (i + 1) % 15 == 0:
            print(f"  ⏳ 已获取K线 {i+1}/{len(pool_stocks)}...", file=sys.stderr)

    # 2) 逐只均线验证
    results = []
    for st in pool_stocks:
        close_prices = kline_cache.get(st["code"])
        if not close_prices or len(close_prices) < 20:
            results.append({**st, "d20": None, "rps": None, "ma10_dev": None, "ma20_dev": None,
                            "bias6": None, "structure": "数据不足", "entry": "⚪ 数据不足",
                            "ma20_slope": None, "dd20": None})
            continue
        d20, rps = fsl.calc_rps(close_prices, all_d20)
        ma5 = fsl.calc_ma(close_prices, 5)
        ma10 = fsl.calc_ma(close_prices, 10)
        ma20 = fsl.calc_ma(close_prices, 20)
        ma60 = fsl.calc_ma(close_prices, 60)
        ma120 = fsl.calc_ma(close_prices, 120)
        ma250 = fsl.calc_ma(close_prices, 250)
        close = close_prices[-1]
        bias6 = (close / ma5 - 1) * 100 if ma5 else None
        structure, entry = fsl.classify_ma(close, ma5, ma10, ma20, ma60, ma120, ma250, bias6, rps)
        ma10_dev = (close / ma10 - 1) * 100 if ma10 else None
        ma20_dev = (close / ma20 - 1) * 100 if ma20 else None
        # 稳定性指标（中大市值+涨得稳，RPS 仅参考）
        # MA20斜率 = 当前MA20 相对 20日前MA20 变化率（中期趋势方向）
        ma20_slope = None
        if len(close_prices) >= 40:
            ma20_prev = fsl.calc_ma(close_prices[:-20], 20)
            if ma20_prev:
                ma20_slope = (ma20 / ma20_prev - 1) * 100
        # 20日最大回撤 = 近21个收盘价内峰谷最大跌幅 %
        dd20 = None
        if len(close_prices) >= 21:
            window = close_prices[-21:]
            peak = window[0]
            dd_max = 0.0
            for c in window[1:]:
                if c > peak:
                    peak = c
                if peak > 0:
                    dd_max = max(dd_max, (1 - c / peak) * 100)
            dd20 = dd_max
        # 活跃股 RPS 不设门槛（大市值稳定龙头 RPS 天然偏低）：
        # RPS<50 判为观察，交由精筛用稳定性三条件把关
        if structure == "活跃股" and entry.startswith("⚪ 一般"):
            entry = "🟡 观察（站上MA20，趋势质量待精筛确认）"
        # 存储K线数据供场景识别用
        st["_close_prices"] = close_prices
        # 真龙头入场判定采用 MA10 四线分级口径（与精筛 L2 完全一致）：
        # 偏离MA10≤4% 优选(贴线) / ≤8% 优选(偏开) / ≤15% 回踩观察 / >15% 或 MA20>25% 乖离警示；
        # 活跃股保留结构观察标签
        if structure == "真龙头":
            dev10a = abs(ma10_dev) if ma10_dev is not None else None
            dev20a = abs(ma20_dev) if ma20_dev is not None else None
            if dev10a is None:
                entry = "⚪ 数据不足"
            elif dev10a <= 4 and (dev20a is None or dev20a <= 25):
                entry = "🟢 优选(贴线)（沿MA10，最佳买点）"
            elif dev10a <= 8 and (dev20a is None or dev20a <= 25):
                entry = "🟢 优选(偏开)（可介入/等回踩）"
            elif dev10a <= 15 and (dev20a is None or dev20a <= 25):
                entry = "🟡 回踩观察（等回踩MA10）"
            else:
                entry = "🟠 乖离警示（偏离MA10>15%或MA20>25%，勿追）"
        results.append({**st, "d20": d20, "rps": rps, "ma10_dev": ma10_dev, "ma20_dev": ma20_dev, "bias6": bias6,
                        "ma20_slope": ma20_slope, "dd20": dd20,
                        "structure": structure, "entry": entry})
    return results


# ================= 场景识别（市场场景匹配） =================

def identify_market_scene(results: List[dict], theme_name: str) -> dict:
    """
    基于龙头股均线结构、乖离、量能识别当前市场场景。

    返回：{
        "scene": "趋势启动" | "趋势中继" | "高潮加速" | "退潮反弹" | "混沌期",
        "basis": [依据列表],
        "action": "首仓介入" | "加仓" | "减仓" | "不参与" | "观望",
        "stop": "MA20下方2%" | "MA20" | "-" | "-" | "-"
    }
    """
    if not results:
        return {"scene": "混沌期", "basis": ["无候选数据"], "action": "观望", "stop": "-"}

    # 统计真龙头和活跃股的数量和乖离分布
    true_leaders = [r for r in results if r["structure"] == "真龙头"]
    active_stocks = [r for r in results if r["structure"] == "活跃股"]

    # 计算平均乖离
    dev10s = [abs(r["ma10_dev"]) for r in results if r["ma10_dev"] is not None]
    dev20s = [abs(r["ma20_dev"]) for r in results if r["ma20_dev"] is not None]
    avg_dev10 = sum(dev10s) / len(dev10s) if dev10s else None
    avg_dev20 = sum(dev20s) / len(dev20s) if dev20s else None

    # 统计高乖离比例（BIAS_6 > 15%的占比）
    high_bias = [r for r in results if r["bias6"] is not None and r["bias6"] > 15]
    high_bias_ratio = len(high_bias) / len(results) if results else 0

    # 统计超跌反弹比例
    oversold = [r for r in results if r["structure"] == "超跌反弹"]
    oversold_ratio = len(oversold) / len(results) if results else 0

    # 统计MA20斜率分布
    slopes = [r["ma20_slope"] for r in results if r["ma20_slope"] is not None]
    avg_slope = sum(slopes) / len(slopes) if slopes else None
    positive_slope_ratio = sum(1 for s in slopes if s > 0) / len(slopes) if slopes else 0

    basis = []

    # 场景判断逻辑
    # 1. 高潮加速：高乖离占比大 + 高斜率
    if high_bias_ratio > 0.3 and avg_slope is not None and avg_slope > 2:
        basis.append(f"高乖离占比{high_bias_ratio:.0%}（BIAS_6>15%的股票较多）")
        basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势陡峭）")
        if avg_dev10 is not None and avg_dev10 > 10:
            basis.append(f"平均偏离MA10{avg_dev10:.1f}%（乖离过大）")
        return {"scene": "高潮加速", "basis": basis, "action": "减仓", "stop": "-"}

    # 2. 趋势中继：有真龙头 + 均值回归（乖离适中）
    if true_leaders and avg_dev10 is not None and 3 <= avg_dev10 <= 10:
        basis.append(f"真龙头{len(true_leaders)}只，活跃股{len(active_stocks)}只")
        basis.append(f"平均偏离MA10{avg_dev10:.1f}%（均值回归区域）")
        if avg_slope is not None and avg_slope > 0:
            basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势向上）")
        return {"scene": "趋势中继", "basis": basis, "action": "加仓", "stop": "MA20"}

    # 3. 趋势启动：有真龙头 + 乖离偏大（刚突破加速）
    if true_leaders and avg_dev10 is not None and avg_dev10 > 10:
        basis.append(f"真龙头{len(true_leaders)}只（刚突破/加速中）")
        basis.append(f"平均偏离MA10{avg_dev10:.1f}%（突破乖离）")
        if avg_slope is not None and avg_slope > 0:
            basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势向上）")
        return {"scene": "趋势启动", "basis": basis, "action": "首仓介入", "stop": "MA20下方2%"}

    # 4. 退潮反弹：超跌反弹占比大 + 斜率低
    if oversold_ratio > 0.5:
        basis.append(f"超跌反弹占比{oversold_ratio:.0%}（大部分股票在年线下方反弹）")
        if avg_slope is not None:
            basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势偏弱）")
        return {"scene": "退潮反弹", "basis": basis, "action": "不参与", "stop": "-"}

    # 5. 混沌期：无真龙头 + 超跌反弹和活跃股混杂
    if not true_leaders:
        basis.append(f"无真龙头（全部为活跃股/超跌反弹）")
        if oversold_ratio > 0.3:
            basis.append(f"超跌反弹占比{oversold_ratio:.0%}")
        if avg_dev10 is not None:
            basis.append(f"平均偏离MA10{avg_dev10:.1f}%（方向不明）")
        return {"scene": "混沌期", "basis": basis, "action": "观望", "stop": "-"}

    # 默认：混沌期
    basis.append(f"真龙头{len(true_leaders)}只/活跃股{len(active_stocks)}只/超跌反弹{len(oversold)}只")
    return {"scene": "混沌期", "basis": basis, "action": "观望", "stop": "-"}


def fmt_pct(v, digits=2, plus=True):
    if v is None:
        return "N/A"
    s = f"{v:.{digits}f}%"
    return f"+{s}" if plus and v > 0 else s


def render_theme(theme: dict, results: List[dict], pool_size: int, n_a: int, n_b: int,
                 concepts: List[str], sectors: List[dict], sector_meta: Optional[dict] = None,
                 concept_watch: Optional[List[dict]] = None, n_constituents: int = 0) -> str:
    """渲染单条主线的分析章节"""
    status = theme["status"]
    emoji = {"active": "🟢", "fading": "🟡", "peaking": "🟠", "ended": "⚫"}.get(status, "⚪")
    def _fmt_sec(s):
        if isinstance(s, str):
            m = re.match(r'(.+)\(([a-z]+\d+),', s)
            if m:
                return f"{m.group(1)}({m.group(2)})"
            return s
        return f"{s.get('name','')}({s.get('code','')})"
    sec_names = "、".join(_fmt_sec(s) for s in sectors) or "无板块"
    con_names = "、".join(concepts) or "无概念"
    out = []
    out.append(f"## {emoji} {theme['theme_name']}（状态：{status}）")
    out.append("")
    out.append(f"- **覆盖板块：** {sec_names}")
    out.append(f"- **覆盖概念：** {con_names}")
    # 板块强度过滤展示（剔除/降权不静默）
    sm = sector_meta or {}
    sec_notes = []
    for name, code, bl, ret5 in sm.get("removed", []):
        ret_s = f"{ret5:+.1f}%" if ret5 is not None else "N/A"
        sec_notes.append(f"🔻 剔除 {name}({code})：板块破位 break_level={bl}（5日{ret_s}），成分股不入池")
    for name in sm.get("weak", []):
        sec_notes.append(f"⚠️ 降权 {name}：板块弱（破MA5 或 MA20斜率≤0），名额减半")
    if sec_notes:
        out.append("")
        out.append("**板块强度过滤：**")
        for n in sec_notes:
            out.append(f"- {n}")
    out.append(f"- **股票池：** {pool_size} 只（成分基线 {n_constituents} + 板块大市值龙头 {n_a} + 表格1上榜股 {n_b}，去重）")
    cw = concept_watch or []
    if cw:
        out.append(f"- **概念观察分组：** {len(cw)} 只（表格1概念命中但**非主线板块成分股**，仅跟踪观察，不参与板块内对比/精筛/alpha 计算）")
    out.append("")
    out.append("### 龙头验证（均线照妖镜）")
    out.append("")
    # 主表只列有信号的：真龙头/活跃股（剔除⚪一般噪音，数据不足不上表）
    main_rows = [r for r in results if "🔴 剔除" not in r["entry"] and "⚪ 一般" not in r["entry"] and "数据不足" not in r["structure"]]
    out.append("| 股票名称 | 代码 | 来源 | 流通市值(亿) | 20日涨幅 | MA20斜率 | 20日回撤 | RPS_池内 | 5日涨幅 | MA10偏离 | MA20偏离 | BIAS6 | 均线结构 | 入场判定 |")
    out.append("|:--------|:---:|:----|:----------:|:-------:|:-----:|:-----:|:-----:|:------:|:------:|:------:|:----:|:----|:----|")
    for st in sorted(main_rows, key=lambda x: (x.get("ltsz") or 0), reverse=True):
        rps_s = f"{st['rps']:.1f}" if st["rps"] is not None else "N/A"
        slope_s = fmt_pct(st["ma20_slope"]) if st["ma20_slope"] is not None else "N/A"
        dd_s = f"{st['dd20']:.1f}%" if st["dd20"] is not None else "N/A"
        out.append(
            f"| {st['name']} | {st['code']} | {st.get('src','')} | "
            f"{st['ltsz']:.1f} | {fmt_pct(st['d20'])} | {slope_s} | {dd_s} | {rps_s} | {fmt_pct(st['zdf5'])} | "
            f"{fmt_pct(st['ma10_dev'])} | {fmt_pct(st['ma20_dev'])} | {fmt_pct(st['bias6'])} | {st['structure']} | {st['entry']} |"
        )
    out.append("")

    # 入场机会聚焦
    focus = [r for r in results if any(t in r["entry"] for t in ("🟢 优选", "🟡 回踩", "🟡 观察"))]
    if focus:
        out.append("### 🎯 入场机会聚焦")
        out.append("")
        out.append("| 股票名称 | 代码 | 来源 | 流通市值(亿) | 20日涨幅 | MA20斜率 | 20日回撤 | RPS_池内 | MA10偏离 | MA20偏离 | BIAS6 | 均线结构 | 入场判定 |")
        out.append("|:--------|:---:|:----|:----------:|:-------:|:-----:|:-----:|:-----:|:------:|:------:|:----:|:----|:----|")
        for st in sorted(focus, key=lambda x: (0 if "🟢 优选" in x["entry"] else 1 if "🟡 回踩" in x["entry"] else 2)):
            rps_s = f"{st['rps']:.1f}" if st["rps"] is not None else "N/A"
            slope_s = fmt_pct(st["ma20_slope"]) if st["ma20_slope"] is not None else "N/A"
            dd_s = f"{st['dd20']:.1f}%" if st["dd20"] is not None else "N/A"
            out.append(
                f"| {st['name']} | {st['code']} | {st.get('src','')} | {st['ltsz']:.1f} | {fmt_pct(st['d20'])} | {slope_s} | {dd_s} | {rps_s} | "
                f"{fmt_pct(st['ma10_dev'])} | {fmt_pct(st['ma20_dev'])} | {fmt_pct(st['bias6'])} | {st['structure']} | {st['entry']} |"
            )
        out.append("")

    # === 场景匹配 ===
    scene = identify_market_scene(results, theme["theme_name"])
    scene_emoji = {"趋势启动": "🚀", "趋势中继": "📈", "高潮加速": "⚡", "退潮反弹": "🔄", "混沌期": "🌫️"}.get(scene["scene"], "❓")
    out.append("### 🎯 市场场景匹配")
    out.append("")
    out.append(f"**当前场景：{scene_emoji} {scene['scene']}**")
    out.append("")
    out.append("**判断依据：**")
    for b in scene["basis"]:
        out.append(f"- {b}")
    out.append("")
    out.append(f"**建议操作：{scene['action']}**（止损：{scene['stop']}）")
    out.append("")

    # 场景化追高判断（场景匹配版）
    out.append("**场景化追高判断：**")
    out.append("")
    out.append("| 场景 | 乖离含义 | 判定 |")
    out.append("|:----|:--------|:----|")
    scene_lines = {
        "趋势启动": "| 趋势启动（刚突破） | BIAS_6 8~15%是正常突破乖离 | ✅ 可以追，回踩MA10是加仓点 |",
        "趋势中继": "| 趋势中继（回踩确认） | BIAS_6 3~8%是回踩后的正常位置 | ✅ 可以追，缩量回踩是确认信号 |",
        "高潮加速": "| 高潮加速（连续大涨） | BIAS_6>15%是加速赶顶信号 | ❌ 不能追，持有者减仓 |",
        "退潮反弹": "| 退潮反弹（跌多弹一下） | BIAS_6可能<10%但均线空头 | ❌ 不能追，反弹是卖点 |",
        "混沌期": "| 混沌期（方向不明） | 乖离无参考意义 | ❌ 观望，等主线出现 |",
    }
    out.append(scene_lines.get(scene["scene"], "| 混沌期 | 乖离无参考意义 | ❌ 观望 |"))
    out.append("")

    # 剔除清单
    dropped = [r for r in results if "🔴 剔除" in r["entry"]]
    if dropped:
        out.append("### 🚫 已剔除（超跌反弹/弱势）")
        out.append("")
        out.append("| 股票名称 | 代码 | 来源 | 流通市值(亿) | RPS_池内 | 均线结构 | 剔除原因 |")
        out.append("|:--------|:---:|:----|:----------:|:-----:|:----|:----|")
        for st in dropped:
            rps_s = f"{st['rps']:.1f}" if st["rps"] is not None else "N/A"
            out.append(
                f"| {st['name']} | {st['code']} | {st.get('src','')} | {st['ltsz']:.1f} | {rps_s} | "
                f"{st['structure']} | {st['entry'].replace('🔴 剔除（','').rstrip('）')} |"
            )
        out.append("")
    # 概念来源跟踪分组（非板块成分股，仅观察）
    if cw:
        out.append("### 👁️ 概念来源跟踪（非板块成分股，仅观察）")
        out.append("")
        out.append("> 以下股票由表格1概念命中进入候选，但**不属于主线覆盖板块成分股**——存在行业归属错配风险（概念标签伪影，如化工股借\"军工/航母\"概念上榜），不参与板块内龙头判定与精筛量化，仅跟踪观察，待行业归属确认后再评估。")
        out.append("")
        out.append("| 股票名称 | 代码 | 来源 | 流通市值(亿) | 当日涨跌 | 5日涨幅 | 20日涨幅 |")
        out.append("|:--------|:---:|:----|:----------:|:-------:|:------:|:------:|")
        for st in sorted(cw, key=lambda x: (x.get("ltsz") or 0), reverse=True):
            out.append(
                f"| {st['name']} | {st['code']} | {st.get('src','')} | {st['ltsz']:.1f} | "
                f"{fmt_pct(st['zdf'], digits=1)} | {fmt_pct(st['zdf5'], digits=1)} | {fmt_pct(st['zdf20'], digits=1)} |"
            )
        out.append("")
    return "\n".join(out)


async def main():
    if len(sys.argv) < 2:
        print("用法: python3 fetch_theme_leaders.py <YYYYMMDD> [--top-n 10] [--min-ltsz 300]", file=sys.stderr)
        sys.exit(1)
    date_compact = sys.argv[1]
    top_n = 10
    min_ltsz = 300.0
    if "--top-n" in sys.argv:
        top_n = int(sys.argv[sys.argv.index("--top-n") + 1])
    if "--min-ltsz" in sys.argv:
        min_ltsz = float(sys.argv[sys.argv.index("--min-ltsz") + 1])

    date_db = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    plate_file = os.path.join(PLATE_DIR, f"plate_{date_compact}.md")
    if not os.path.exists(plate_file):
        print(f"❌ 板块文件不存在: {plate_file}", file=sys.stderr)
        sys.exit(2)

    themes = load_themes_from_db(date_db)
    if not themes:
        print(f"❌ industry_themes 无 {date_db} 主线数据", file=sys.stderr)
        sys.exit(0)
    # 16:30 技术确认是否回填：break_level IS NULL = 16:30 断档/未回填 → 按 0 保守处理（不误杀），报告头标注
    # 建池口径：active/peaking 且未破位（break_level<2）；peaking=热度退坡但趋势+逻辑完好，照常建池
    pool_all = [t for t in themes if t["status"] in ("active", "peaking")]
    unconfirmed = [t for t in pool_all if t.get("break_level") is None]
    if unconfirmed:
        print(f"⚠️ {len(unconfirmed)} 条 active/peaking 主线无 16:30 技术确认（break_level 未回填），按未破位保守处理", file=sys.stderr)
    degraded = [t for t in pool_all if (t.get("break_level") or 0) >= 2]
    if degraded:
        print(f"🔻 **技术破位降级观察：** {len(degraded)} 条 active/peaking 主线被 16:30 技术确认拦截（break_level>=2，不产可执行清单）", file=sys.stderr)
        for t in degraded:
            bl = t.get("break_level") or 0
            sig = t.get("advice_signal") or "—"
            print(f"  - {t['theme_name']}（break_level={bl}, 建议=[{sig}]）", file=sys.stderr)
    active = [t for t in pool_all if (t.get("break_level") or 0) < 2]
    if not active:
        print("⚠️ 当日无 active/peaking 且未破位主线，无分析对象（仍输出说明性报告，见 stdout）", file=sys.stderr)

    table1 = parse_plate_table1_full(plate_file)

    # P2 §4.2④：跨链重复股票集（多链共振打标，强化信号非错误，不剔除）
    multichain_stocks = load_multichain_stock_set()

    # 头部
    run_label = "自动执行" if date_compact == datetime.now().strftime("%Y%m%d") else f"手动补跑（{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}）"
    print(f"# 🏭 主线龙头股分析 {date_compact}")
    print("")
    print(f"> 生成时间：{run_label}")
    print(f"> 数据来源：腾讯财经接口 + industry_themes（{date_db}）")
    print(f"> 分析口径：**主线维度** —— 板块和上榜股票仅作为股票池来源，按 active/peaking 主线聚类输出龙头")
    print(f"> 选股原则：**中大市值(≥{min_ltsz:.0f}亿) + 涨得稳** —— MA20斜率>0（趋势向上）、20日回撤可控（防追高接盘）；RPS 仅作参考，不设硬门槛")
    print("")
    status_line = "；".join(
        f"{'🟢' if t['status']=='active' else '🟡' if t['status']=='fading' else '🟠' if t['status']=='peaking' else '⚫'}"
        f"{t['theme_name']}({t['status']}"
        f"{'🔻破位' if t['status'] in ('active','peaking') and (t.get('break_level') or 0) >= 2 else ''})"
        for t in themes)
    print(f"> **当日主线全景：** {status_line}")
    if unconfirmed:
        print(f"> **⚠️ 无 16:30 技术确认：** {len(unconfirmed)} 条 active/peaking 主线 break_level 未回填（16:30 断档/未执行），按未破位保守处理、建池不受拦截：")
        for t in unconfirmed:
            print(f">   - {t['theme_name']}（break_level=NULL, 建议=—）")
        print("")
    if degraded:
        print(f"> **🔻 技术破位降级：** {len(degraded)} 条 active/peaking 主线被 16:30 技术确认拦截（break_level≥2），本轮不建池、不产可执行清单：")
        for t in degraded:
            print(f">   - {t['theme_name']}（break_level={t.get('break_level') or 0}, 建议=[{t.get('advice_signal') or '—'}]）")
        print("")
    print(f"> **本次覆盖：** {len(active)} 条 active/peaking 且未破位主线（fading/ended 及破位主线交由 16:00/16:30 分析）")
    print("")
    print("---")
    print("")

    # 逐主线
    all_focus = []  # (theme_name, name, code, ltsz, rps, structure, entry)
    for theme in active:
        print(f"⏳ 处理主线: {theme['theme_name']} ...", file=sys.stderr)
        pool_stocks, concept_watch, concepts, sectors, sector_meta, pool_meta = await build_pool(theme, table1, top_n, min_ltsz)
        # 概念观察分组也补市值（单独分组仅跟踪展示，同样按统一市值门槛过滤）
        await enrich_ltsz(concept_watch)
        concept_watch = [s for s in concept_watch if s.get("ltsz", 0) >= min_ltsz]
        await enrich_ltsz(pool_stocks)
        # 统一市值门槛：来源B（表格1上榜股）补市值后同样过滤 min_ltsz（来源A 已在 build_pool 过滤）
        pool_stocks = [s for s in pool_stocks if s.get("ltsz", 0) >= min_ltsz]
        # P2 §4.2④：跨链重复候选打标 🔄 多链共振（强化信号非错误，不剔除）
        for s in pool_stocks:
            if s["code"] in multichain_stocks and "🔄 多链共振" not in str(s.get("src", "")):
                s["src"] = str(s.get("src", "")) + " 🔄 多链共振"
        n_constituents = sum(1 for s in pool_stocks if str(s.get("src", "")).startswith("成分"))
        n_a = sum(1 for s in pool_stocks if str(s.get("src", "")).startswith("板块"))
        n_b = len(pool_stocks) - n_a - n_constituents
        if not pool_stocks:
            print(f"⚠️ 主线 {theme['theme_name']} 股票池为空，跳过", file=sys.stderr)
            # 即使股票池为空，也输出场景匹配信息（含板块强度过滤说明）
            _emoji = {"active": "🟢", "fading": "🟡", "peaking": "🟠", "ended": "⚫"}.get(theme["status"], "⚪")
            print(f"## {_emoji} {theme['theme_name']}（状态：{theme['status']}）")
            print("")
            sm = sector_meta or {}
            if sm.get("removed") or sm.get("weak"):
                print("**板块强度过滤：**")
                for name, code, bl, ret5 in sm.get("removed", []):
                    ret_s = f"{ret5:+.1f}%" if ret5 is not None else "N/A"
                    print(f"- 🔻 剔除 {name}({code})：板块破位 break_level={bl}（5日{ret_s}），成分股不入池")
                for name in sm.get("weak", []):
                    print(f"- ⚠️ 降权 {name}：板块弱（破MA5 或 MA20斜率≤0），名额减半")
            if concept_watch:
                print(f"- **概念观察分组：** {len(concept_watch)} 只（表格1概念命中但**非主线板块成分股**，仅跟踪观察）")
            print("")
            # F3 §二：空池口径披露
            pm = pool_meta or {}
            n_baseline = pm.get("baseline_count", 0)
            n_removed = len(sm.get("removed", [])) + len(sm.get("t3_skipped", []))
            top3 = pm.get("baseline_top3", [])
            print("### ⚠️ 口径说明")
            print("")
            print(f"- **空池原因：** 成分基线 {n_baseline} 只 / 板块被剔或被破位 {n_removed} 个")
            if top3:
                top3_str = "、".join(f"{t['name']}({t['ltsz']:.1f}亿)" for t in top3)
                print(f"- **链内成分基线流通市值 Top3：** {top3_str}")
            else:
                print(f"- **链内成分基线流通市值 Top3：** 无（成分基线为空）")
            if top3 and all(t["ltsz"] < min_ltsz for t in top3):
                print(f"- **口径冲突提示：** 链内成分流通市值均 <{min_ltsz:.0f} 亿，与「中大市值≥{min_ltsz:.0f}亿」建池口径冲突，属口径必然结果，非数据异常")
            print("")
            print("### 🎯 市场场景匹配（工具E）")
            print("")
            print("**当前场景：⏭️ 无候选数据** — 股票池为空，无法识别场景")
            print("")
            if concept_watch:
                print("### 👁️ 概念来源跟踪（非板块成分股，仅观察）")
                print("")
                print("> 以下股票由表格1概念命中进入候选，但**不属于主线覆盖板块成分股**——存在行业归属错配风险（概念标签伪影，如化工股借\"军工/航母\"概念上榜），不参与板块内龙头判定与精筛量化，仅跟踪观察。")
                print("")
                print("| 股票名称 | 代码 | 来源 | 流通市值(亿) | 当日涨跌 | 5日涨幅 | 20日涨幅 |")
                print("|:--------|:---:|:----|:----------:|:-------:|:------:|:------:|")
                for st in sorted(concept_watch, key=lambda x: (x.get("ltsz") or 0), reverse=True):
                    print(
                        f"| {st['name']} | {st['code']} | {st.get('src','')} | {st['ltsz']:.1f} | "
                        f"{fmt_pct(st['zdf'], digits=1)} | {fmt_pct(st['zdf5'], digits=1)} | {fmt_pct(st['zdf20'], digits=1)} |"
                    )
                print("")
            print("---")
            print("")
            continue
        results = await analyze_pool(pool_stocks)
        print(render_theme(theme, results, len(pool_stocks), n_a, n_b, concepts, sectors, sector_meta, concept_watch, n_constituents))
        print("")
        print("---")
        print("")
        for r in results:
            if any(t in r["entry"] for t in ("🟢 优选", "🟡 回踩", "🟡 观察")):
                all_focus.append((theme["theme_name"], r["name"], r["code"], r["ltsz"], r["rps"], r["structure"], r["entry"]))

    # 全场汇总
    print("# 🎯 全场入场机会汇总（主线维度）")
    print("")
    if all_focus:
        def sort_key(x):
            rank = {"🟢 优选": 0, "🟡 回踩": 1, "🟡 观察": 2}.get(x[6][:4], 3)
            return (rank, -(x[3] if x[3] is not None else -1))  # 同级按流通市值降序（中大市值优先）
        print("| 主线 | 股票名称 | 代码 | 流通市值(亿) | RPS_池内 | 均线结构 | 入场判定 |")
        print("|:----|:--------|:---:|:----------:|:-----:|:----|:----|")
        for tname, name, code, ltsz, rps, structure, entry in sorted(all_focus, key=sort_key):
            rps_s = f"{rps:.1f}" if rps is not None else "N/A"
            print(f"| {tname} | {name} | {code} | {ltsz:.1f} | {rps_s} | {structure} | {entry} |")
        print("")
        top = [x for x in all_focus if "🟢 优选" in x[6]]
        pullback = [x for x in all_focus if "🟡 回踩" in x[6]]
        watch = [x for x in all_focus if "🟡 观察" in x[6]]
        if top:
            print(f"**🌟 优选区（🟢真龙头+可介入）：** " + "、".join(f"{x[1]}({x[0]})" for x in top))
        if pullback:
            print(f"**👀 回踩观察（🟡真龙头等回踩）：** " + "、".join(f"{x[1]}({x[0]})" for x in pullback))
        if watch:
            watch_str = "、".join(f"{x[1]}({x[0]})" for x in watch[:5])
            more = f" 等{len(watch)}只" if len(watch) > 5 else ""
            print(f"**🔎 强度观察（🟡活跃股等MA60走强）：** {watch_str}{more}")
        print("")
    else:
        if not active:
            print("当日无 active/peaking 且未破位主线（全部被 16:30 技术确认拦截或无候选池），详见头部降级段落。")
        else:
            print("今日无满足入场条件的龙头标的（均线验证后全部剔除或数据不足）。")
        print("")


if __name__ == "__main__":
    asyncio.run(main())
