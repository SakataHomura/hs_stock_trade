#!/usr/bin/env python3
"""
outside_pool_leaders.py — 池外候选主线个股筛选（18:45 环节，2026-08-24 从 18:00 池外提示拆分独立）
================================================================
在 18:30 outside_pool_themes.py 主线归纳之后运行，读取其 JSON 结论。

定位：对 strong/moderate 信号候选主线，筛出**存在入场机会的股票和龙头股**，
产出行之有效的候选清单（与 fetch_theme_leaders.py / fetch_faded_watch_leaders.py 口径一致）。

  【龙头】均线结构=真龙头（MA20/MA60多头排列，价格>MA20>MA60，MA20斜率>0），按市值降序
  【全量候选】入场判定分级（🟢贴线/🟡偏开/🟡回踩区），🟢优先
  【剔除】超跌反弹/BIAS6>15%/MA20乖离>25%/活跃股MA20斜率≤0/回撤>20%/20日涨幅≤0
  【场景匹配】趋势启动🚀/趋势中继📈/高潮加速⚡/退潮反弹🔄/混沌期🌫️

口径：全部客观量价（K线/实时行情），禁用主力资金等推算指标；无合格机会时如实空仓，不硬凑候选。

输入： data/outside_pool_themes_YYYYMMDD.json（18:30 主线归纳结论）
输出： data/outside_pool_leaders_YYYYMMDD.md（龙头 + 全量候选 + 剔除清单 + 场景匹配）

用法：
  python3 outside_pool_leaders.py <YYYYMMDD>
  bash scripts/run_outside_pool_leaders_cron.sh [YYYYMMDD]   # cron/nohup 入口
"""

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional, Tuple

# ================= 路径配置（生产：/home/agentuser/data/定时任务打包） =================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
DATA_DIR = os.path.join(WORKSPACE, "data")

# 只对强/中信号做个股筛选
SCREEN_STRENGTHS = ("strong", "moderate")

# 市值门槛（亿）
LTSZ_MIN = 300


def load_json(date_compact: str) -> dict:
    """读取 18:30 主线归纳 JSON 结论"""
    json_file = os.path.join(DATA_DIR, f"outside_pool_themes_{date_compact}.json")
    if not os.path.exists(json_file):
        print(f"❌ 找不到 {json_file}（18:30 主线归纳未生成）", file=sys.stderr)
        sys.exit(1)
    with open(json_file, encoding="utf-8") as f:
        return json.load(f)


# ================= 网络接口（腾讯） =================

def get_qt_quote(code: str) -> Optional[dict]:
    """获取个股实时行情（腾讯 qt.gtimg.cn）"""
    url = f"https://qt.gtimg.cn/q={code}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="ignore")
        for line in raw.split("\n"):
            line = line.strip()
            if "=" not in line or not line.startswith("v_"):
                continue
            payload = line.split("=", 1)[1].strip().strip('"')
            fields = payload.split("~")
            if len(fields) < 46:
                continue

            def _f(v):
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return 0.0

            return {
                "name": fields[1],
                "code": f"sh{fields[2]}" if line.startswith("v_sh") else f"sz{fields[2]}",
                "price": _f(fields[3]),
                "ltsz": _f(fields[44]),
                "turnover": _f(fields[38]),
                "pe": float(fields[39]) if fields[39] else None,
                "zdf": _f(fields[32]),
            }
    except Exception:
        return None


def get_kline(code: str, limit: int = 60) -> List[dict]:
    """获取个股K线"""
    url = f"https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get?code={code}&ktype=day&fqtype=qfq&limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=10).read()
        data = json.loads(raw.decode("utf-8"))
        nodes = data.get("data", {}).get("nodes", [])
        result = []
        for n in nodes:
            result.append({
                "date": n.get("date", ""),
                "open": float(n.get("open", 0)),
                "last": float(n.get("last", 0)),
                "high": float(n.get("high", 0)),
                "low": float(n.get("low", 0)),
                "volume": float(n.get("volume", 0)),
                "amount": float(n.get("amount", 0)),
            })
        return result
    except Exception:
        return []


def get_sector_leaders(code: str, top_n: int = 10) -> List[dict]:
    """获取板块成分股（腾讯 rank/hs/getBoardRankList 接口，按当日涨幅排序取 top_n）"""
    url = f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList?board_code={code}&sort_type=priceRatio&direct=down&count={top_n}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=10).read()
        data = json.loads(raw.decode("utf-8"))
        if data.get("code") != 0 or "data" not in data:
            return []
        rank_list = data["data"].get("rank_list", [])
        stocks = []
        for item in rank_list:
            c_code = item.get("code", "")
            c_name = item.get("name", "")
            if not c_code or not c_name:
                continue
            # 代码补前缀
            if c_code.startswith("6"):
                c_code = f"sh{c_code}"
            elif c_code.startswith("0") or c_code.startswith("3"):
                c_code = f"sz{c_code}"
            stocks.append({
                "code": c_code,
                "name": c_name,
                "ltsz": float(item.get("ltsz", 0) or 0),
                "zdf": float(item.get("zdf", 0) or 0),
            })
        return stocks[:top_n]
    except Exception as e:
        print(f"    ⚠️ 获取板块{code}成分股失败: {e}", file=sys.stderr)
        return []


# ================= 个股分析（与 fetch_theme_leaders.py 口径一致） =================

def calc_ma(klines: List[dict], days: int) -> Optional[float]:
    """计算MA均线"""
    if len(klines) < days:
        return None
    return sum(k["last"] for k in klines[-days:]) / days


def calc_ma_slope(klines: List[dict], days: int = 20) -> Optional[float]:
    """计算MA斜率（%），用最近2个MA值"""
    if len(klines) < days + 1:
        return None
    ma_prev = calc_ma(klines[:-1], days)
    ma_curr = calc_ma(klines, days)
    if ma_prev and ma_prev > 0:
        return (ma_curr - ma_prev) / ma_prev * 100
    return None


def calc_highest(klines: List[dict], days: int = 20) -> Optional[float]:
    """计算N日最高价"""
    if len(klines) < days:
        return None
    return max(k["high"] for k in klines[-days:])


def calc_drawdown(klines: List[dict], days: int = 20) -> Optional[float]:
    """计算N日最大回撤（%）"""
    if len(klines) < days:
        return None
    high = calc_highest(klines, days)
    if high and high > 0:
        return (klines[-1]["last"] - high) / high * 100
    return None


def determine_structure(klines: List[dict], ma20: float, ma20_slope: float) -> str:
    """判断均线结构：真龙头/活跃股/超跌反弹"""
    if not klines or ma20 is None:
        return "数据不足"
    last = klines[-1]["last"]
    # 检查MA20/MA60多头排列
    ma60 = calc_ma(klines, 60)
    if ma60 and last > ma20 > ma60 and ma20_slope > 0:
        return "真龙头"
    elif ma20_slope > 0:
        return "活跃股"
    else:
        return "超跌反弹"


def calc_bias6(klines: List[dict], ma10: float) -> Optional[float]:
    """计算BIAS6（6日乖离率）"""
    if len(klines) < 6 or ma10 is None or ma10 == 0:
        return None
    ref = klines[-6]["last"]
    return (klines[-1]["last"] - ref) / ref * 100


def analyze_stock(code: str, name: str, src: str) -> Optional[dict]:
    """对单只股票做完整分析（与 refine_theme_leaders.py / fetch_faded_watch_leaders.py 口径一致）"""
    klines = get_kline(code, limit=60)
    if len(klines) < 20:
        return None

    quote = get_qt_quote(code)
    if not quote:
        return None

    ltsz = quote.get("ltsz", 0)
    if ltsz < LTSZ_MIN:
        return None  # 市值门槛

    last = klines[-1]["last"]
    ma10 = calc_ma(klines, 10)
    ma20 = calc_ma(klines, 20)
    ma20_slope = calc_ma_slope(klines, 20)
    d20 = (last - klines[-21]["last"]) / klines[-21]["last"] * 100 if len(klines) >= 21 else None
    d5 = (last - klines[-6]["last"]) / klines[-6]["last"] * 100 if len(klines) >= 6 else None
    dd20 = calc_drawdown(klines, 20)
    high20 = calc_highest(klines, 20)
    bias6 = calc_bias6(klines, ma10)

    structure = determine_structure(klines, ma20, ma20_slope)

    # 量比
    vol_ratio = None
    if len(klines) >= 6:
        vols = [k["volume"] for k in klines[-6:-1]]
        if any(v > 0 for v in vols):
            avg5 = sum(vols) / len(vols)
            if avg5 > 0:
                vol_ratio = klines[-1]["volume"] / avg5

    # 距高点
    dist_high = None
    if high20 and high20 > 0:
        dist_high = (last - high20) / high20 * 100

    # 距MA10/MA20
    ma10_dev = (last - ma10) / ma10 * 100 if ma10 else None
    ma20_dev = (last - ma20) / ma20 * 100 if ma20 else None

    # 入场判定（与 refine_theme_leaders.py 一致）
    entry = "观望"
    if structure == "超跌反弹":
        entry = "🔴 剔除（超跌反弹）"
    elif bias6 is not None and bias6 > 15:
        entry = "🔴 剔除（BIAS6>15%）"
    elif ma20_dev is not None and abs(ma20_dev) > 25:
        entry = "🔴 剔除（MA20乖离>25%）"
    elif ma10_dev is not None:
        if abs(ma10_dev) <= 4:
            entry = "🟢 优选（贴线）"
        elif abs(ma10_dev) <= 8:
            entry = "🟡 优选（偏开）"
        elif abs(ma10_dev) <= 15:
            entry = "🟡 回踩区"

    # 稳定性检查
    if entry.startswith("🟢") or entry.startswith("🟡"):
        if structure == "活跃股":
            if ma20_slope is not None and ma20_slope <= 0:
                entry = "🔴 剔除（MA20斜率向下）"
            elif dd20 is not None and abs(dd20) > 20:
                entry = "🔴 剔除（回撤>20%）"
            elif d20 is not None and d20 <= 0:
                entry = "🔴 剔除（20日涨幅≤0）"

    return {
        "code": code,
        "name": name,
        "src": src,
        "ltsz": ltsz,
        "d20": d20,
        "d5": d5,
        "dd20": dd20,
        "ma20_slope": ma20_slope,
        "ma10_dev": ma10_dev,
        "ma20_dev": ma20_dev,
        "bias6": bias6,
        "structure": structure,
        "entry": entry,
        "vol_ratio": vol_ratio,
        "turnover": quote.get("turnover"),
        "pe": quote.get("pe"),
        "dist_high": dist_high,
    }


# ================= 任务2：个股筛选 =================

def screen_candidates(theme_name: str, sectors: List[dict]) -> Tuple[List[dict], List[dict], List[dict]]:
    """对候选主线进行个股筛选。

    返回：(leaders, candidates, dropped)
    - leaders: 龙头股（真龙头，按市值降序）
    - candidates: 全量候选（🟢/🟡 入场判定）
    - dropped: 剔除清单
    """
    sec_codes = [s["code"] for s in sectors if s.get("code")]
    sec_name_map = {s["code"]: s.get("name", "") for s in sectors}

    # 获取每个板块的成分股（标注实际来源板块，多板块主线不误标为第1个板块）
    all_stocks = []
    seen_codes = set()
    for code in sec_codes:
        stocks = get_sector_leaders(code, top_n=10)
        for s in stocks:
            if s["code"] not in seen_codes:
                seen_codes.add(s["code"])
                s["_src_board"] = sec_name_map.get(code, "")
                all_stocks.append(s)

    # 分析每只股票
    results = []
    for s in all_stocks:
        src_label = f"板块[{s.get('_src_board','')}]" if s.get("_src_board") else f"主线[{theme_name}]"
        result = analyze_stock(s["code"], s["name"], src_label)
        if result:
            results.append(result)

    # 分类
    # 龙头：真龙头（不管entry是什么，真龙头就是龙头）
    leaders = [r for r in results if r["structure"] == "真龙头"]
    # 全量候选：未被剔除的股票（🟢或🟡）
    candidates = [r for r in results if r["entry"].startswith("🟢") or r["entry"].startswith("🟡")]
    dropped = [r for r in results if r["entry"].startswith("🔴")]

    # 龙头排序：按市值降序
    leaders.sort(key=lambda x: x["ltsz"], reverse=True)
    candidates.sort(key=lambda x: (0 if "🟢" in x["entry"] else 1, -x["ltsz"]))

    return leaders, candidates, dropped


# ================= 场景匹配（工具E） =================

def identify_scene(results: List[dict], theme_name: str) -> dict:
    """基于个股均线结构识别市场场景（与 fetch_theme_leaders.py 口径一致）"""
    if not results:
        return {"scene": "混沌期", "basis": ["无候选数据"], "action": "观望", "stop": "-"}

    true_leaders = [r for r in results if r.get("structure") == "真龙头"]
    active_stocks = [r for r in results if r.get("structure") == "活跃股"]

    dev10s = [abs(r.get("ma10_dev") or 0) for r in results if r.get("ma10_dev") is not None]
    avg_dev10 = sum(dev10s) / len(dev10s) if dev10s else None

    high_bias = [r for r in results if r.get("bias6") is not None and r["bias6"] > 15]
    high_bias_ratio = len(high_bias) / len(results) if results else 0

    oversold = [r for r in results if r.get("structure") == "超跌反弹"]
    oversold_ratio = len(oversold) / len(results) if results else 0

    slopes = [r.get("ma20_slope") or 0 for r in results if r.get("ma20_slope") is not None]
    avg_slope = sum(slopes) / len(slopes) if slopes else None

    basis = []

    if high_bias_ratio > 0.3 and avg_slope is not None and avg_slope > 2:
        basis.append(f"高乖离占比{high_bias_ratio:.0%}")
        basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势陡峭）")
        return {"scene": "高潮加速", "basis": basis, "action": "减仓", "stop": "-"}

    if true_leaders and avg_dev10 is not None and 3 <= avg_dev10 <= 10:
        basis.append(f"真龙头{len(true_leaders)}只，活跃股{len(active_stocks)}只")
        basis.append(f"平均偏离MA10 {avg_dev10:.1f}%（均值回归区域）")
        if avg_slope is not None and avg_slope > 0:
            basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势向上）")
        return {"scene": "趋势中继", "basis": basis, "action": "加仓", "stop": "MA20"}

    if true_leaders and avg_dev10 is not None and avg_dev10 > 10:
        basis.append(f"真龙头{len(true_leaders)}只（刚突破/加速中）")
        basis.append(f"平均偏离MA10 {avg_dev10:.1f}%（突破乖离）")
        if avg_slope is not None and avg_slope > 0:
            basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势向上）")
        return {"scene": "趋势启动", "basis": basis, "action": "首仓介入", "stop": "MA20下方2%"}

    if oversold_ratio > 0.5:
        basis.append(f"超跌反弹占比{oversold_ratio:.0%}")
        if avg_slope is not None:
            basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势偏弱）")
        return {"scene": "退潮反弹", "basis": basis, "action": "不参与", "stop": "-"}

    if not true_leaders:
        basis.append(f"无真龙头（全部为活跃股/超跌反弹）")
        if avg_dev10 is not None:
            basis.append(f"平均偏离MA10 {avg_dev10:.1f}%（方向不明）")
        return {"scene": "混沌期", "basis": basis, "action": "观望", "stop": "-"}

    return {"scene": "混沌期", "basis": basis, "action": "观望", "stop": "-"}


# ================= 报告渲染 =================

def fmt_pct(v, digits=2, plus=True):
    if v is None:
        return "N/A"
    s = f"{v:.{digits}f}%"
    return f"+{s}" if plus and v > 0 else s


def render_report(date_compact: str, payload: dict,
                  screen_results: Dict[str, dict]) -> str:
    """渲染任务2报告（个股筛选）"""
    out = []
    if date_compact != datetime.now().strftime("%Y%m%d"):
        run_label = f"手动补跑 {date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    else:
        run_label = "自动执行"

    out.append(f"# 🎯 池外候选主线个股筛选报告 {date_compact}")
    out.append("")
    out.append(f"> 生成时间：{run_label}")
    out.append("> 环节：18:45（2026-08-24 从18:00池外提示拆分独立）")
    out.append(f"> 数据源：18:30 主线归纳结论（outside_pool_themes_{date_compact}.json）+ 腾讯K线/实时行情")
    out.append("> 口径：龙头=真龙头（MA20/MA60多头排列）；候选=🟢贴线/🟡偏开/🟡回踩区；剔除=超跌反弹/高乖离/高回撤/无趋势")
    out.append("> 市值门槛：流通市值≥300亿；全部客观量价，禁用主力资金等推算指标")
    out.append("")

    # ========== 任务2：个股筛选 ==========
    out.append("## 🎯 候选主线个股筛选")
    out.append("")

    # 只对主线级做个股筛选（过滤掉小/无执行性主线）
    themes = payload.get("themes", [])
    screen_targets = [t for t in themes
                      if (t.get("track_level") == "main") or
                      (t.get("track_level") in (None, "") and t.get("strength") in SCREEN_STRENGTHS)]
    if not screen_targets:
        out.append("**无主线级候选（18:30 成色过滤后无主线），跳过个股筛选。**")
        out.append("")
    else:
        for t in screen_targets:
            out.append(f"### {t['theme_name']}（{'🟢' if t['strength']=='strong' else '🟡'} {t['strength']}，评分{t['score']:.1f}）")
            out.append("")

            res = screen_results.get(t["theme_name"])
            if not res:
                out.append("**⚠️ 筛选失败/无数据。**")
                out.append("")
                continue

            leaders = res["leaders"]
            candidates = res["candidates"]
            dropped = res["dropped"]

            # 场景匹配
            scene = identify_scene(candidates + leaders, t["theme_name"])
            scene_emoji = {"趋势启动": "🚀", "趋势中继": "📈", "高潮加速": "⚡", "退潮反弹": "🔄", "混沌期": "🌫️"}.get(scene["scene"], "❓")
            out.append(f"> **市场场景：{scene_emoji} {scene['scene']}** — {scene['action']}（止损：{scene['stop']}）")
            out.append(">")
            for b in scene["basis"]:
                out.append(f"> - {b}")
            out.append("")

            # 龙头股
            if leaders:
                out.append("#### 👑 龙头股")
                out.append("")
                out.append("| 股票名称 | 代码 | 来源 | 流通市值(亿) | 20日涨幅 | MA20斜率 | 20日回撤 | 距MA10 | 距MA20 | BIAS6 | 均线结构 | 入场判定 |")
                out.append("|:--------|:---:|:----|:----------:|:-------:|:-----:|:-----:|:----:|:----:|:----:|:----|:----|")
                for st in leaders:
                    out.append(f"| {st['name']} | {st['code']} | {st['src']} | {st['ltsz']:.0f} | {fmt_pct(st['d20'])} | {fmt_pct(st['ma20_slope'])} | {fmt_pct(st['dd20'])} | {fmt_pct(st['ma10_dev'])} | {fmt_pct(st['ma20_dev'])} | {fmt_pct(st['bias6'])} | {st['structure']} | {st['entry']} |")
                out.append("")
            else:
                out.append("**👑 龙头股：无**（未识别出符合条件的真龙头）")
                out.append("")

            # 全量候选
            if candidates:
                out.append("#### 📋 全量候选（存在入场机会）")
                out.append("")
                out.append("| 股票名称 | 代码 | 来源 | 流通市值(亿) | 20日涨幅 | MA20斜率 | 20日回撤 | 距MA10 | 距MA20 | BIAS6 | 量比 | 换手率 | PE | 均线结构 | 入场判定 |")
                out.append("|:--------|:---:|:----|:----------:|:-------:|:-----:|:-----:|:----:|:----:|:----:|:----:|:----:|:---:|:----|:----|")
                for st in candidates:
                    vol_s = f"{st['vol_ratio']:.2f}" if st['vol_ratio'] is not None else "N/A"
                    tur_s = f"{st['turnover']:.1f}%" if st['turnover'] is not None else "N/A"
                    pe_s = f"{st['pe']:.0f}" if st['pe'] is not None else "N/A"
                    out.append(f"| {st['name']} | {st['code']} | {st['src']} | {st['ltsz']:.0f} | {fmt_pct(st['d20'])} | {fmt_pct(st['ma20_slope'])} | {fmt_pct(st['dd20'])} | {fmt_pct(st['ma10_dev'])} | {fmt_pct(st['ma20_dev'])} | {fmt_pct(st['bias6'])} | {vol_s} | {tur_s} | {pe_s} | {st['structure']} | {st['entry']} |")
                out.append("")

                # 一句话依据
                out.append("**筛选依据：**")
                out.append("")
                for st in candidates:
                    basis_parts = []
                    if st["vol_ratio"] is not None:
                        basis_parts.append(f"量比{st['vol_ratio']:.1f}")
                    if st["turnover"] is not None:
                        basis_parts.append(f"换手{st['turnover']:.1f}%")
                    if st["ma10_dev"] is not None:
                        basis_parts.append(f"偏离MA10 {st['ma10_dev']:+.1f}%")
                    basis_str = "，".join(basis_parts) if basis_parts else "基础指标正常"
                    out.append(f"- **{st['name']}**：{st['structure']} {st['entry']}（{basis_str}）")
                out.append("")
            else:
                out.append("**📋 全量候选：无**（未通过市值/结构/乖离筛选）")
                out.append("")

            # 剔除清单
            if dropped:
                out.append("#### 🚫 已剔除")
                out.append("")
                out.append(f"剔除 {len(dropped)} 只：")
                kill_summary = defaultdict(int)
                for d in dropped:
                    kill_summary[d["entry"]] += 1
                summary = "；".join(f"{k}({v}只)" for k, v in sorted(kill_summary.items(), key=lambda x: x[1], reverse=True))
                out.append(f"> {summary}")
                out.append("")

            out.append("---")
            out.append("")

    # ========== 汇总 ==========
    out.append("## 📌 汇总")
    out.append("")

    if screen_targets:
        out.append("| 候选主线 | 强度 | 评分 | 龙头数 | 候选数 | 场景 |")
        out.append("|:--------|:---:|:----:|:-----:|:-----:|:----|")
        for t in screen_targets:
            res = screen_results.get(t["theme_name"])
            if not res:
                out.append(f"| {t['theme_name']} | {'🟢' if t['strength']=='strong' else '🟡'} {t['strength']} | {t['score']:.1f} | - | - | ⚠️失败 |")
                continue
            scene = identify_scene(res["candidates"] + res["leaders"], t["theme_name"])
            scene_emoji = {"趋势启动": "🚀", "趋势中继": "📈", "高潮加速": "⚡", "退潮反弹": "🔄", "混沌期": "🌫️"}.get(scene["scene"], "❓")
            out.append(f"| {t['theme_name']} | {'🟢' if t['strength']=='strong' else '🟡'} {t['strength']} | {t['score']:.1f} | {len(res['leaders'])} | {len(res['candidates'])} | {scene_emoji} {scene['scene']} |")
        out.append("")
    else:
        out.append("无强/中信号候选主线。")
        out.append("")

    return "\n".join(out)


# ================= 主流程 =================

def main():
    parser = argparse.ArgumentParser(description="池外候选主线个股筛选（18:45 任务2）")
    parser.add_argument("date", help="YYYYMMDD 格式日期")
    args = parser.parse_args()

    date_compact = args.date

    # 读取 18:30 主线归纳结论
    print("📥 读取 18:30 主线归纳结论...", file=sys.stderr)
    payload = load_json(date_compact)
    themes = payload.get("themes", [])
    print(f"  {len(themes)} 条候选主线（快照日 {payload.get('snap_date')}）", file=sys.stderr)

    # 只对主线级（track_level=="main"）做个股筛选；老JSON无track_level时回退到强/中信号
    screen_targets = [t for t in themes
                      if (t.get("track_level") == "main") or
                      (t.get("track_level") in (None, "") and t.get("strength") in SCREEN_STRENGTHS)]
    print(f"  主线级 {len(screen_targets)} 条进入个股筛选（过滤掉小/无执行性主线）", file=sys.stderr)

    # 个股筛选
    print("\n🔍 个股筛选...", file=sys.stderr)
    screen_results = {}
    for t in screen_targets:
        print(f"  🔍 {t['theme_name']}...", file=sys.stderr)
        leaders, candidates, dropped = screen_candidates(t["theme_name"], t["sectors"])
        screen_results[t["theme_name"]] = {
            "leaders": leaders,
            "candidates": candidates,
            "dropped": dropped,
        }
        print(f"    龙头{len(leaders)}只，候选{len(candidates)}只，剔除{len(dropped)}只", file=sys.stderr)

    # 生成报告
    print("\n📝 生成报告...", file=sys.stderr)
    report = render_report(date_compact, payload, screen_results)

    output_file = os.path.join(DATA_DIR, f"outside_pool_leaders_{date_compact}.md")
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n✅ 报告已生成: {output_file}", file=sys.stderr)
    print(report)


if __name__ == "__main__":
    main()