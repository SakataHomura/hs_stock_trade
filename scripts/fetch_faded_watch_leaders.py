#!/usr/bin/env python3
"""
fetch_faded_watch_leaders.py — 监控池主线机会分析（18:00 环节6，2026-08-22 重构为"启动可能+交易机会三分类"）
================================================================
在 17:30 faded_theme_monitor.py 更新 faded_theme_watch 之后运行。
定位：**监控存在主线启动可能的板块（A/B级），并筛选存在回撤买入点的股票。**

  【第一维度 主线启动可能分级】前置过滤，不在风口的主线不产候选：
    🅰️ 高：reviving / revived / 当日 revive_candidate（状态机已确认重启苗头）→ 必分析
    🅱️ 中：watching + trend_ok 且存在3日涨股比均值<40% 且 当日涨股比<60%（未扩散）的 trend_ok 板块（低位启动特征：指数走强、多数个股未跟涨，当日未普涨扩散）→ 重点分析
    🅲️ 观察：trend_ok 但板块已扩散（3日均≥40% 或 当日≥60%）；或 trend_weak/trend_broken → 只列状态，不出候选

  【第二维度 个股交易机会】三分类判定（2026-08-22 二次重构：替代"回撤四要素"——原口径要求20日涨幅≥8%+回撤3~15%，对监控池慢牛/低位启动主线系统性误杀，长期空仓不具备可用性；现改为位置×涨幅联动，贴顶≠追高）：
    硬杀（任一即剔除）：结构非真龙头/活跃股、市值<300亿、PE<0、MA20斜率≤0、距MA20<-3%（跌破趋势）、放量下跌(当日阴线且量比≥1.5)、天量(量比>2.0)、近3日涨停、20日涨幅<-8%（深跌无趋势，防接飞刀）
    分级（位置×涨幅联动）：
      🟢 均线低吸：距20日高点≤-3%（有回撤空间）→ 沿上升MA20低吸
      🟡 平台启动：距高点>-3% 且 20日涨幅<8%（没涨过贴顶=低位横盘整理待突破，不是追高）
      🟠 追高警示：20日涨幅≥8% 且 距高点>-3%（真涨过又贴顶=追高，等回撤，不主推）
    输出：🟢/🟡 为可交易候选；🟠 只警示不主推；🔴 剔除（无候选时如实空仓）

全部客观可观测数据（K线/行情），禁用主力资金等推算型指标。
目标是识别监控池内**可交易的机会**（低吸/平台启动），而不是等一个极窄的回撤窗口——但硬杀规则保证不推破位/追高/无趋势的票；真无机会时如实空仓。

用法：
    python3 fetch_faded_watch_leaders.py <YYYYMMDD>
    bash scripts/run_faded_watch_leaders_cron.sh [YYYYMMDD]   # cron/nohup 入口

输入： portfolio.faded_theme_watch（17:30 更新）+ data/faded_theme_watch_YYYYMMDD.md（板块信号摘要）
输出： data/faded_watch_leaders_YYYYMMDD.md（按主线启动级别排序：A/B级产候选，C级只列状态）
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_theme_leaders as ftl       # analyze_pool 复用（RPS+均线+MA20斜率+20日回撤）
import refine_theme_leaders as rtl      # get_quote_batch / get_kline_volumes / refine_stock 复用
import fetch_sector_leaders as fsl      # get_all_constituents

DATA_DIR = "/home/agentuser/data/定时任务打包/data"
DB_ARGS = ["mysql", "-u", "wish", "-p<DB_PASSWORD>", "portfolio", "-N", "-e"]

# 2026-08-22 全A基准改造参数（与 faded_theme_monitor.py 保持一致）
RANK_DAILY_TOP_HALF = 62      # B级产候选全A位次硬条件：当日/20日位次前50%（124×0.5）
RANK_LOW_ZGB = 40.0           # 涨股比<40% = 低位启动特征（3日均值）
RANK_TODAY_MAX = 60.0         # 当日涨股比<60% = 未扩散（当日普涨齐涨属反抽/脉冲，非低位启动）


# ================= 监控池读取 =================

def mysql_q(sql: str) -> List[str]:
    """执行 mysql 查询，返回每行字符串列表"""
    proc = subprocess.run(DB_ARGS + [sql], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        print(f"⚠️ mysql 查询失败: {proc.stderr.strip()}", file=sys.stderr)
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def load_watch_themes() -> List[dict]:
    """读 faded_theme_watch 池，返回值得跟踪主线列表"""
    sql = ("SELECT theme_name, related_sectors, fade_reason, watch_status, watch_days, "
           "last_signal_type, reconcile FROM faded_theme_watch "
           "WHERE watch_status IN ('watching','reviving','revived')")
    themes = []
    for line in mysql_q(sql):
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        theme_name, related_sectors, fade_reason, watch_status, watch_days, last_signal_type, reconcile = parts
        reconcile = None if reconcile == "NULL" else reconcile
        # 值得跟踪：趋势完好/破位观察(watching+trend_ok/weak/broken) 或 触发重启(reviving/revived/revive_candidate)
        if watch_status in ("reviving", "revived") or last_signal_type == "revive_candidate":
            keep = True
        elif watch_status == "watching" and last_signal_type in ("trend_ok", "trend_weak", "trend_broken"):
            keep = True
        else:
            keep = False
        if keep:
            themes.append({
                "theme": theme_name, "related_sectors": related_sectors,
                "fade_reason": fade_reason, "watch_status": watch_status,
                "watch_days": watch_days, "last_signal_type": last_signal_type,
                "reconcile": reconcile,
            })
    return themes


def parse_sectors(related_sectors: str) -> List[dict]:
    """解析 related_sectors JSON 的 sectors 板块列表 [{name, code}]"""
    try:
        data = json.loads(related_sectors)
        return [{"name": s.get("name", ""), "code": s.get("code", "")}
                for s in data.get("sectors", []) if s.get("code")]
    except (json.JSONDecodeError, AttributeError):
        return []


def _parse_ratio_text(text):
    """解析板块行涨股比字段：
    '50.0%' → (50.0, 50.0, [50.0])
    '50%→45%→38%均44.3%' → (38.0, 44.3, [50.0,45.0,38.0])
    'N/A' → (None, None, [])
    均X% 仅用于均值，不混入 hist（防 hist[-1]=当日值被均值污染）
    """
    if not text or text == "N/A":
        return None, None, []
    m = re.search(r"均([\d.]+)%", text)
    mean = float(m.group(1)) if m else None
    hist = [float(x) for x in re.findall(r"([\d.]+)%", re.sub(r"均[\d.]+%", "", text))]
    if not hist:
        return None, None, []
    if mean is None:
        mean = round(sum(hist) / len(hist), 1)
    return hist[-1], mean, hist


def load_theme_report(date_compact: str) -> Dict[str, dict]:
    """解析 data/faded_theme_watch_YYYYMMDD.md，按主线提取板块信号摘要。
    支持 2026-08-22 新版板块行格式（属性分号分隔、无嵌套括号）：
      航运港口(trend_ok; 涨股比50%→50%均50%; RankD7/124; RankD20 29/124; 量比1.2; ⭐)
    与旧格式兼容：航运港口(trend_ok,涨股比50.0%)
    """
    path = os.path.join(DATA_DIR, f"faded_theme_watch_{date_compact}.md")
    result = {}
    if not os.path.exists(path):
        return result
    with open(path, encoding="utf-8") as f:
        content = f.read()
    current = None
    for line in content.splitlines():
        # 主线行: - 👀 **交运产业链** → `trend_weak` 上榜数=7 低位板块=...
        m = re.match(r"^- .*?\*\*(.+?)\*\*.*?→ `(\w+)` 上榜数=(\d+)", line)
        if m:
            current = m.group(1).strip()
            result[current] = {
                "signal": m.group(2), "list_count": m.group(3),
                "sector_map": {}, "note": "",
            }
            continue
        if current and "退潮原因:" in line:
            result[current]["note"] = line.strip().lstrip("- ").replace("退潮原因: ", "")
        if current and "板块:" in line:
            sector_info = line.strip().lstrip("- ").replace("板块: ", "")
            # 板块块形如 name(attrs)，attrs 内无嵌套括号（新版分号分隔，旧版逗号分隔）
            for sm in re.finditer(r"([^()]+?)\(([^()]*)\)", sector_info):
                sname = sm.group(1).strip().lstrip("; ").strip()
                attrs_text = sm.group(2).strip()
                parts = [p.strip() for p in attrs_text.split(";")] if attrs_text else []
                if len(parts) == 1 and "," in (parts[0] if parts else ""):
                    parts = [p.strip() for p in parts[0].split(",")]
                sig, zgb_today, zgb3, hist = None, None, None, []
                rankd = rankd20 = lb = None
                strong = False
                for kv in parts:
                    if not kv:
                        continue
                    if kv.startswith("trend_"):
                        sig = kv
                    elif kv.startswith("涨股比"):
                        zgb_today, zgb3, hist = _parse_ratio_text(kv.replace("涨股比", ""))
                    elif kv.startswith("RankD20"):
                        mm = re.search(r"(\d+)/", kv)
                        rankd20 = int(mm.group(1)) if mm else None
                    elif kv.startswith("RankD") or kv.startswith("Rank"):
                        mm = re.search(r"(\d+)/", kv)
                        rankd = int(mm.group(1)) if mm else None
                    elif kv.startswith("量比"):
                        mm = re.search(r"[\d.]+", kv)
                        lb = float(mm.group()) if mm else None
                    elif kv == "⭐":
                        strong = True
                if not sig:
                    continue
                result[current]["sector_map"][sname] = {
                    "signal": sig,
                    "zgb": zgb_today, "zgb3": zgb3, "zgb_hist": hist,
                    "rankd": rankd, "rankd20": rankd20, "lb": lb, "strong": strong,
                    "ok": sig == "trend_ok",  # 仅 trend_ok 可产候选；trend_weak 走弱同样不建池
                }
            result[current]["sector_info"] = sector_info
    return result


# ================= 主线启动可能分级 =================

def theme_launch_level(theme: dict, report: dict) -> Tuple[str, str, Optional[List[str]]]:
    """对监控池主线定级启动可能：🅰️高 / 🅱️中 / 🅲️观察。
    （2026-08-22 全A基准改造：返回 focus_codes——行业级强信号时仅指定板块产候选）

    依据（顺序从严）：
      🅰️ 状态机已确认重启苗头：reviving / revived / 当日 revive_candidate
      🅱️ 行业级强信号（改造②）：任一板块 ⭐（当日全A前20% + 量比≥1.2，且趋势非 broken）→ 该板块单独产候选
      🅱️ 低位启动 + 全A位次硬条件（改造①）：存在 trend_ok 板块 3日涨股比均值<40% 且 当日涨股比<60%（未扩散）
          且 (当日位次前50% 或 20日位次前50%+当日涨股比≥40%)
      🅲️ 其余：trend_ok 但全A位次不达标 / trend_weak / 信号不足——只跟踪不产候选
    返回 (级别, 一句话依据, focus_codes)
    """
    t = theme["theme"]
    ws = theme.get("watch_status", "")
    sig_type = theme.get("last_signal_type", "")
    sig = report.get(t, {})
    theme_signal = sig.get("signal", sig_type)
    sector_map = sig.get("sector_map", {})

    if theme.get("reconcile") == "mainline_conflict":
        return "R", "已回归16:00主线池，本池指数层破位不适用", None

    # 🅰️ 状态机重启信号优先
    if ws in ("reviving", "revived") or sig_type == "revive_candidate":
        return "A", f"状态机重启信号({ws}/{sig_type})，优先分析", None

    if ws != "watching":
        return "C", f"信号不足（watch_status={ws} / signal={theme_signal}），只跟踪不产候选", None

    # 🅱️ 行业级强信号（改造②）：⭐ 板块（当日全A前20%+放量）→ 该板块单独产候选
    strong_sectors = [sname for sname, si in sector_map.items()
                      if si.get("strong") and si.get("signal") != "trend_broken"]
    if strong_sectors:
        return "B", "行业级强信号（⭐）：" + "、".join(strong_sectors) + "（当日全A前20%+量比≥1.2），单独产候选", strong_sectors

    if theme_signal != "trend_ok":
        return "C", f"主线信号{theme_signal}，无重启/行业级强信号，只跟踪不产候选", None

    # 🅱️ 低位启动特征 + 全A相对位次硬条件（改造①）
    low_zgb_ok = []
    reason_diffs = []   # trend_ok 但未达标的真实原因（用于 C 级说明）
    for sname, sinfo in sector_map.items():
        if sinfo.get("signal") != "trend_ok":
            continue
        zgb_today = sinfo.get("zgb")
        # 当日涨股比≥60% = 当日普涨扩散/脉冲（非前排带动启动），与17:30 revive_candidate 同口径拦截
        if zgb_today is not None and zgb_today >= RANK_TODAY_MAX:
            reason_diffs.append(f"{sname} 当日涨股比{zgb_today:.0f}%≥60%（当日普涨扩散/脉冲，非低位启动）")
            continue
        # 3日涨股比均值<40%（数据不足1天时用当日值）
        ratio = sinfo.get("zgb3") if sinfo.get("zgb3") is not None else sinfo.get("zgb")
        rankd = sinfo.get("rankd")
        rankd20 = sinfo.get("rankd20")
        if ratio is not None and ratio >= RANK_LOW_ZGB:
            reason_diffs.append(f"{sname} 3日涨股比{ratio:.0f}%≥40%（板块已扩散）")
            continue
        why = None
        if rankd is not None and rankd <= RANK_DAILY_TOP_HALF:
            why = f"当日全A第{rankd}/124"
        elif rankd20 is not None and rankd20 <= RANK_DAILY_TOP_HALF \
                and zgb_today is not None and zgb_today >= RANK_LOW_ZGB:
            why = f"20日全A第{rankd20}/124且当日涨股比{zgb_today:.0f}%"
        if why:
            low_zgb_ok.append(f"{sname}(3日涨股比{ratio if ratio is not None else '?'}%,{why})")
        else:
            reason_diffs.append(
                f"{sname} 全A位次不达标(当日{rankd or '?'}/124,20日{rankd20 or '?'}/124)")
    if low_zgb_ok:
        return "B", "低位启动特征（全A位次达标）：" + "、".join(low_zgb_ok), None
    if reason_diffs:
        return "C", "trend_ok 但低位启动特征不足：" + "；".join(reason_diffs) + "——只跟踪不产候选", None
    return "C", "trend_ok 但全A位次不达标（当日/20日涨幅均未进前50%）或低位特征不足——板块已扩散或相对全A偏弱，只跟踪", None


def theme_level_icon(level: str) -> str:
    return {"A": "🅰️", "B": "🅱️", "C": "🅲️"}.get(level, "🅲️")


# ================= 股票池构建 =================

async def build_pool(sectors: List[dict], report_sectors: Optional[Dict[str, dict]],
                     top_n: int = 10, min_ltsz: float = 300.0) -> Tuple[List[dict], List[str]]:
    """对主线 sectors 板块取大市值成分股（≥300亿），合并去重；返回 (pool, skipped_sectors)。
    report_sectors: 17:30 报告解析的板块信号 {板块名: {signal, ok}}；仅 trend_ok 板块产候选
    （trend_broken 趋势破坏 / trend_weak 走弱 均跳过，不纳入候选）"""
    pool: Dict[str, dict] = {}
    skipped = []
    for sec in sectors:
        code, name = sec.get("code", ""), sec.get("name", "")
        if not code:
            continue
        # 板块信号过滤：trend_broken（趋势破坏）/trend_weak（走弱未破坏）均不纳入龙头候选
        if report_sectors and name in report_sectors:
            if not report_sectors[name]["ok"]:
                skipped.append(f"{name}({report_sectors[name]['signal']})")
                continue
        stocks = await fsl.get_all_constituents(code)
        if not stocks:
            continue
        ranked = []
        for s in stocks:
            c, nm = s.get("code", ""), s.get("name", "")
            if not c or not nm:
                continue
            try:
                ltsz = float(s.get("ltsz", 0))
            except (ValueError, TypeError):
                ltsz = 0.0
            ranked.append({
                "code": c, "name": nm, "ltsz": ltsz,
                "zdf": float(s.get("zdf", 0)), "zxj": float(s.get("zxj", 0)),
                "turnover": float(s.get("turnover", 0)), "hsl": float(s.get("hsl", 0)),
                "pe": s.get("pe_ttm", "N/A"), "src": f"板块[{name}]",
                "zdf5": None, "zdf20": None,
            })
        ranked.sort(key=lambda x: x["ltsz"], reverse=True)
        for st in ranked[:top_n]:
            if st["ltsz"] >= min_ltsz:
                pool[st["code"]] = st
    return list(pool.values()), skipped


# ================= 个股交易机会判定 =================

def pullback_from_high20(klines: List[dict]) -> Optional[float]:
    """距20日高点回撤 %（负值=回撤；None=数据不足）。
    用近20个交易日的最高价做参照，判断当前价离近期顶有多远。
    """
    if not klines or len(klines) < 20:
        return None
    closes = [k.get("last") for k in klines[-20:]]
    if any(v is None or v <= 0 for v in closes):
        return None
    high20 = max(closes)
    cur = closes[-1]
    return (cur / high20 - 1) * 100


def evaluate_trade_setup(st: dict, klines: List[dict], quote: dict) -> dict:
    """交易机会三分类判定（2026-08-22 二次重构，替代回撤四要素）。

    核心：位置×涨幅联动——贴顶≠追高（没涨过贴顶=横盘整理待突破；涨过8%又贴顶才是追高）。
    硬杀：结构不合格/市值<300/PE<0/MA20斜率≤0/跌破MA20/放量下跌/天量/近3日涨停/20日涨幅<-8%。
    分级：🟢均线低吸(pb≤-3%) / 🟡平台启动(贴顶但没涨过) / 🟠追高警示(涨过又贴顶) / 🔴剔除。
    返回 {st, verdict, level, reasons, kills, metrics...}
    """
    reasons, kills = [], []
    name = st["name"]

    # ---- 硬杀：趋势/结构/估值/量能 ----
    structure = st.get("structure", "")
    if structure not in ("真龙头", "活跃股"):
        kills.append(f"结构不合格({structure})")
    ltsz = st.get("ltsz")
    if ltsz is not None and ltsz < 300:
        kills.append(f"市值{ltsz:.0f}亿<300")
    ma20_slope = st.get("ma20_slope")
    if ma20_slope is not None and ma20_slope <= 0:
        kills.append(f"MA20斜率{ma20_slope:+.1f}%不向上")
    ma20_dev = st.get("ma20_dev")
    if ma20_dev is not None and ma20_dev < -3:
        kills.append(f"跌破MA20(距MA20 {ma20_dev:+.1f}%)")
    pe = quote.get("pe") if quote else None
    pe_note = ""
    if pe is not None:
        try:
            if float(pe) < 0:
                kills.append(f"亏损(PE{pe})")
                pe_note = " ⚠️亏损"
        except ValueError:
            pass
    # 深跌无趋势（防接飞刀）：只保留横盘/慢涨/主升三态
    d20 = st.get("d20")
    if d20 is not None and d20 < -8:
        kills.append(f"深跌无趋势(20日涨幅{d20:.0f}%<-8%)")

    # ---- 量能（缩量/温和放量保留，放量下跌/天量/情绪涨停杀） ----
    vol_ratio = None
    if klines and len(klines) >= 6:
        vols = [k.get("volume") for k in klines[-6:-1]]
        if any(v and v > 0 for v in vols):
            avg5 = sum(v for v in vols if v) / len(vols)
            if avg5 > 0:
                vol_ratio = klines[-1].get("volume", 0) / avg5
    today_down = False
    if klines and len(klines) >= 2:
        today_down = klines[-1].get("last", 0) < klines[-2].get("last", 0)
    if vol_ratio is not None:
        if today_down and vol_ratio >= 1.5:
            kills.append(f"放量下跌(量比{vol_ratio:.1f})")
        elif today_down and vol_ratio < 1.0:
            reasons.append(f"缩量回踩(量比{vol_ratio:.1f})")
        elif vol_ratio > 2.0:
            kills.append(f"天量(量比{vol_ratio:.1f})")
        else:
            reasons.append(f"量比{vol_ratio:.1f}")
    else:
        reasons.append("量比N/A")

    # 近3日涨停（情绪票剔除）
    if klines and len(klines) >= 5:
        threshold = 19.5 if st["code"].startswith(("sz30", "sh68", "sz301")) else 9.8
        for i in range(-3, 0):
            prev, cur = klines[i - 1].get("last"), klines[i].get("last")
            if prev and cur and prev > 0 and (cur - prev) / prev * 100 >= threshold:
                kills.append("近3日涨停(情绪票)")
                break

    # ---- 位置×涨幅联动分级 ----
    pb = pullback_from_high20(klines)
    pb_valid = pb is not None
    if pb is not None:
        if pb <= -3:
            reasons.append(f"回撤{pb:.1f}%（有回撤空间，沿均线低吸）")
        elif d20 is not None and d20 >= 8:
            reasons.append(f"涨过{d20:.0f}%又贴顶(距高点{pb:+.1f}%)——追高警示")
        else:
            reasons.append(f"贴顶整理(距高点{pb:+.1f}%，20日仅涨{d20:.0f}%——横盘待突破)")

    verdict = "❌ 剔除" if kills else "✅ 保留"
    if verdict == "✅ 保留":
        if pb_valid and pb <= -3:
            level = "均线低吸"
        elif pb_valid and d20 is not None and d20 >= 8:
            level = "追高警示"
        else:
            level = "平台启动"
    else:
        level = "剔除"

    # 换手率（qt 实测快照，与判定口径一致）
    turnover = quote.get("turnover") if quote else None

    return {
        "st": st, "verdict": verdict, "level": level,
        "kills": kills, "reasons": reasons, "pe_note": pe_note,
        "vol_ratio": vol_ratio, "pb": pb, "pe": pe,
        "turnover": turnover,
    }


def level_rank(level: str) -> int:
    return {"均线低吸": 0, "平台启动": 1, "追高警示": 2, "剔除": 3}.get(level, 3)


def render_theme(idx: int, theme: dict, report: dict, refined: List[dict], kept: List[dict],
                 skipped_sectors: List[str] = None, level: str = "C", level_note: str = "") -> str:
    out = []
    t = theme["theme"]
    emoji = "👀" if theme["watch_status"] == "watching" else "🚀"
    sig = report.get(t, {})
    signal_disp = sig.get("signal", theme.get("last_signal_type", "?"))
    list_cnt = sig.get("list_count", "?")
    lv_icon = theme_level_icon(level)
    degrade = " ⚠️候选不足，继续观察" if level in ("A", "B") and 0 < len(kept) < 2 else ""
    title = f"## {idx}. {lv_icon} {t}（{theme['watch_status']} / {signal_disp} / 上榜数{list_cnt}{degrade}）"
    if theme.get("reconcile") == "mainline_aligned":
        title += "（与16:00主线池方向一致）"
    out.append(title)
    out.append("")
    if sig.get("note"):
        out.append(f"- **退潮原因**：{sig['note']}")
    else:
        out.append(f"- **退潮原因**：{theme.get('fade_reason', '?')}")
    if sig.get("sector_info"):
        out.append(f"- **板块现状**：{sig['sector_info']}")
    if level_note:
        out.append(f"- **启动可能**：{level_note}")
    if skipped_sectors:
        out.append(f"- **跳过板块**：{'、'.join(skipped_sectors)}（趋势破坏/走弱，不纳入候选）")
    out.append("")

    if level == "C":
        out.append("### 🔒 只跟踪不产候选")
        out.append("")
        out.append("- 启动可能不足（trend_weak 或板块已扩散），本轮不深挖个股。等待板块状态升级（revive_candidate/reviving/revived）后再产候选。")
        out.append("")
        return "\n".join(out)

    if not kept:
        out.append("### 🚫 无交易机会")
        out.append("")
        out.append("- 监控池主线本轮无可交易标的（🟢均线低吸/🟡平台启动均无；硬杀规则见剔除摘要）。")
        if refined:
            killed_total = [r for r in refined if r["verdict"] == "❌ 剔除"]
            out.append(f"- 剔除 {len(killed_total)} 只：详见下方剔除摘要。")
        out.append("")
        # 追高区个股（有主升证据但贴顶，不产买点但需告知等回撤）
        chase = [r for r in refined if r["verdict"] == "✅ 保留" and r["level"] == "追高警示"]
        if chase:
            out.append("### 🟠 追高警示（涨过又贴顶，等回撤不做买点）")
            out.append("")
            out.append("| 股票 | 代码 | 来源 | 市值(亿) | 20日涨幅 | 距高点 | 距MA20 | 量比 | PE | 均线结构 |")
            out.append("|:---|:---|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
            for r in chase:
                st = r["st"]
                d20 = f"{st['d20']:+.1f}%" if st.get("d20") is not None else "N/A"
                pb = f"{r['pb']:+.1f}%" if r.get("pb") is not None else "N/A"
                d20m = f"{st['ma20_dev']:+.1f}%" if st.get("ma20_dev") is not None else "N/A"
                vr = f"{r['vol_ratio']:.2f}" if r.get("vol_ratio") is not None else "N/A"
                pe = r.get("pe") if r.get("pe") else "N/A"
                basis = "，".join(r["reasons"]) if r["reasons"] else "贴顶无回撤"
                out.append(f"| {st['name']} | {st['code']} | {st['src']} | {st['ltsz']:.0f} | {d20} | {pb} | {d20m} | {vr} | {pe} | {st['structure']} |")
            out.append("")
            for r in chase:
                st = r["st"]
                basis = "，".join(r["reasons"]) if r["reasons"] else "贴顶无回撤"
                out.append(f"- **{st['name']}**：{st['structure']} 追高警示（{basis}）——回撤至 -3% 下方再评估")
            out.append("")
        out.extend(_killed_summary(refined))
        return "\n".join(out)

    out.append("### 交易机会候选（🟢均线低吸沿上升MA20介入 / 🟡平台启动低位整理待突破）")
    out.append("")
    out.append("| 股票 | 代码 | 来源 | 市值(亿) | 20日涨幅 | 距高点 | 距MA20 | 量比 | 换手率 | PE | 均线结构 | 机会判定 |")
    out.append("|:---|:---|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
    for r in kept:
        st = r["st"]
        d20 = f"{st['d20']:+.1f}%" if st.get("d20") is not None else "N/A"
        pb = f"{r['pb']:+.1f}%" if r.get("pb") is not None else "N/A"
        d20m = f"{st['ma20_dev']:+.1f}%" if st.get("ma20_dev") is not None else "N/A"
        vr = f"{r['vol_ratio']:.2f}" if r.get("vol_ratio") is not None else "N/A"
        tur = f"{r['turnover']:.1f}%" if r.get("turnover") is not None else "N/A"
        pe = r.get("pe") if r.get("pe") else "N/A"
        out.append(f"| {st['name']} | {st['code']} | {st['src']} | {st['ltsz']:.0f} | {d20} | {pb} | {d20m} | {vr} | {tur} | {pe} | {st['structure']} | {r['level']}{r['pe_note']} |")
    out.append("")
    # 一句话依据
    for r in kept:
        st = r["st"]
        basis = "，".join(r["reasons"])
        out.append(f"- **{st['name']}**：{st['structure']} {r['level']}（{basis}）")
    out.append("")
    # 追高区个股（不产买点但需告知：有主升证据但贴顶）
    chase = [r for r in refined if r["verdict"] == "✅ 保留" and r["level"] == "追高警示"]
    if chase:
        out.append("### 🟠 同池追高警示（涨过又贴顶，等回撤不做买点）")
        out.append("")
        for r in chase:
            st = r["st"]
            basis = "，".join(r["reasons"]) if r["reasons"] else "贴顶无回撤"
            out.append(f"- **{st['name']}**：{st['structure']} 追高警示（{basis}）——回撤至 -3% 下方再评估")
        out.append("")
    out.extend(_killed_summary(refined))
    return "\n".join(out)


def _killed_summary(refined: List[dict]) -> List[str]:
    """剔除摘要（kept 空/非空两分支共用，修复'详见下方剔除摘要'死引用）。"""
    out = []
    killed = [r for r in refined if r["verdict"] == "❌ 剔除"]
    if killed:
        from collections import Counter
        cnt = Counter()
        for r in killed:
            for k in r["kills"]:
                cnt[k] += 1
        summary = "；".join(f"{k}({v}只)" for k, v in cnt.most_common(8))
        out.append(f"> 🚫 剔除 {len(killed)} 只（≥300亿候选）：{summary}")
        out.append("")
    return out


# ================= 主流程 =================

async def main():
    if len(sys.argv) < 2:
        print("用法: python3 fetch_faded_watch_leaders.py <YYYYMMDD>", file=sys.stderr)
        sys.exit(1)
    date_compact = sys.argv[1]
    today = datetime.now().strftime("%Y%m%d")
    run_label = "自动执行" if date_compact == today else f"手动补跑 {date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"

    themes = load_watch_themes()
    if not themes:
        print(f"# 退潮监控池主线龙头分析 {date_compact}")
        print("")
        print(f"> {run_label}")
        print("")
        print("今日无值得跟踪的退潮主线（监控池为空或全部 trend_broken/dropped）。")
        print("")
        return

    report_meta = load_theme_report(date_compact)
    # 主线启动可能分级（前置过滤：C级主线不产候选）
    theme_levels = {}
    for th in themes:
        lv, note, focus = theme_launch_level(th, report_meta)
        theme_levels[th["theme"]] = (lv, note, focus)
    active_themes = [t for t in themes if theme_levels[t["theme"]][0] in ("A", "B")]
    c_themes = [t for t in themes if theme_levels[t["theme"]][0] == "C"]
    returned_themes = [t for t in themes if theme_levels[t["theme"]][0] == "R"]

    out_lines = [f"# 退潮监控池主线龙头分析 {date_compact}", "",
                 f"> 环节6（18:00）：{run_label}", "",
                 f"> 数据：faded_theme_watch {date_compact} 快照 + 腾讯成分股/K线/行情接口 + sector_rank_daily 全A排行快照（17:30落库）。",
                 "> 口径：**监控主线启动可能 + 筛出可交易机会**（①B级产候选加全A相对位次硬条件（当日/20日位次前50%）②趋势判定下沉行业级（⭐=任一板块当日全A前20%+放量→单独产候选）③涨股比改3日趋势非单日快照 + 当日<60%未扩散拦截，trend_weak板块不建池）—— 主线维度：🅰️状态机重启信号/🅱️行业级强信号或低位启动特征(3日涨股比<40%且当日<60%未扩散+全A位次达标)产候选，🅲️观察(趋势弱/3日均≥40%或当日≥60%扩散/位次不达标)只跟踪；个股维度：交易机会三分类（硬杀：结构非真龙头/活跃股、市值<300亿、PE<0、MA20斜率≤0、跌破MA20 3%、放量下跌、天量、近3日涨停、20日跌幅>8%——🟢均线低吸(距高点≤-3%沿上升MA20介入)/🟡平台启动(贴顶但20日涨幅<8%，横盘待突破)/🟠追高警示(涨过8%又贴顶，等回撤不主推)）。全部客观量价，禁用主力资金等推算指标；真无机会时如实空仓。", ""]

    all_kept = []
    # 先渲染 C 级主线（只列状态）——保持报告按 原监控池顺序 呈现
    ordered = themes
    for i, theme in enumerate(ordered, 1):
        lv, note, focus_codes = theme_levels[theme["theme"]]
        if lv == "R":
            continue
        sectors = parse_sectors(theme["related_sectors"])
        if focus_codes:
            # 行业级强信号：仅对 ⭐ 板块建池产候选
            sectors = [s for s in sectors if s.get("name") in focus_codes]
            if not sectors:
                # ⭐板块名与成分板块名不一致兜底：不强过滤避免空池
                sectors = parse_sectors(theme["related_sectors"])
        report_sectors = report_meta.get(theme["theme"], {}).get("sector_map")
        # 17:30 报告解析失败（报告缺失/格式变更/主线未解析）：链路断裂，不产候选
        # 防 trend_broken 板块无信号可过滤而静默混入候选池
        if lv in ("A", "B") and not report_sectors:
            lv, note = "C", f"⚠️ 17:30 报告解析失败（{theme['theme']} 无板块信号），链路断裂，本轮不产候选"
        if lv == "C":
            # 跳过建池，仅渲染状态
            out_lines.append(render_theme(i, theme, report_meta, [], [], None, level="C", level_note=note))
            out_lines.append("---")
            out_lines.append("")
            continue
        pool, skipped_sectors = await build_pool(sectors, report_sectors, top_n=10, min_ltsz=300.0)
        if not pool:
            out_lines.append(f"## {i}. {theme_level_icon(lv)} {theme['theme']}（{theme['watch_status']} / {theme.get('last_signal_type','?')}）")
            out_lines.append("")
            out_lines.append("- ⚠️ 板块成分股获取失败或全部 <300亿，本轮无候选。")
            out_lines.append("")
            continue
        analyzed = await ftl.analyze_pool(pool)
        # 交易机会三分类判定（🟢均线低吸/🟡平台启动/🟠追高警示）
        codes = [st["code"] for st in analyzed]
        quote_cache = await rtl.get_quote_batch(codes)
        refined = []
        for st in analyzed:
            klines = await rtl.get_kline_volumes(st["code"], limit=65)
            quote = quote_cache.get(st["code"], {})
            r = evaluate_trade_setup(st, klines, quote)
            refined.append(r)
        # 排序：均线低吸 > 平台启动 > 追高警示 > 剔除；同级按距-5%(低吸目标)近再市值
        refined.sort(key=lambda r: (level_rank(r["level"]),
                                    -abs((r["pb"] or 0) - (-5)),
                                    -(r["st"]["ltsz"] if r["st"]["ltsz"] is not None else -1)))
        kept = [r for r in refined if r["verdict"] == "✅ 保留" and r["level"] in ("均线低吸", "平台启动")]
        # 同源去重：同一来源板块最多2只（沿用防单beta重复暴露）；组内排序=机会等级优先→市值降序
        from collections import defaultdict
        src_groups: Dict[str, List[dict]] = defaultdict(list)
        for r in kept:
            src_groups[r["st"]["src"]].append(r)
        tmp_kept = []
        dedup_dropped = 0
        for src, items in src_groups.items():
            lvl_order = {"均线低吸": 0, "平台启动": 1, "追高警示": 2, "剔除": 3}
            items.sort(key=lambda r: (
                lvl_order.get(r["level"], 4),
                -abs((r.get("pb") or 0) - (-5)),
                -(r["st"]["ltsz"] if r["st"]["ltsz"] is not None else -1),
            ))
            dedup_dropped += max(0, len(items) - 2)
            tmp_kept.extend(items[:2])
        if dedup_dropped:
            print(f"> 🔁 同源去重 {dedup_dropped} 只（同一来源板块按机会等级/市值保留Top2）", file=sys.stderr)
        tmp_kept.sort(key=lambda r: (level_rank(r["level"]),
                                     -abs((r.get("pb") or 0) - (-5)),
                                     -(r["st"]["ltsz"] if r["st"]["ltsz"] is not None else -1)))
        kept = tmp_kept[:4]  # 每条主线最多 4 只
        out_lines.append(render_theme(i, theme, report_meta, refined, kept, skipped_sectors, level=lv, level_note=note))
        out_lines.append("---")
        out_lines.append("")
        all_kept.extend((theme["theme"], k) for k in kept)

    # 尾部汇总
    if all_kept:
        from collections import Counter
        theme_cnt = Counter(tname for tname, _ in all_kept)
        out_lines.append("## 📌 交易机会汇总")
        out_lines.append("")
        out_lines.append("| 主线 | 股票 | 代码 | 市值(亿) | 20日涨幅 | 距高点 | 机会判定 | 备注 |")
        out_lines.append("|:---|:---|:---|:---:|:---:|:---:|:---:|:---:|")
        for tname, r in all_kept:
            st = r["st"]
            d20 = f"{st['d20']:+.1f}%" if st.get("d20") is not None else "N/A"
            pb = f"{r['pb']:+.1f}%" if r.get("pb") is not None else "N/A"
            note = "⚠️候选不足" if theme_cnt[tname] < 2 else ""
            out_lines.append(f"| {tname} | {st['name']} | {st['code']} | {st['ltsz']:.0f} | {d20} | {pb} | {r['level']}{r['pe_note']} | {note} |")
        out_lines.append("")

    # 池外板块分析已于 2026-08-24 迁移至独立环节：18:30 outside_pool_themes.py（主线归纳）+ 18:45 outside_pool_leaders.py（个股筛选）

    if returned_themes:
        out_lines.append("")
        out_lines.append(
            f"## ✅ 已回归主线（{len(returned_themes)} 条）："
            f"{' / '.join(theme['theme'] for theme in returned_themes)}")
        out_lines.append("")
        for theme in returned_themes:
            out_lines.append(f"- **{theme['theme']}**：已回归 16:00 主线池（active），本池指数层破位不适用，见 16:00/16:45 主线口径")
        out_lines.append("")

    # 破位观察中（trend_broken 第1-2天，不产候选但需可见）
    broken_obs = [t for t in themes
                  if t.get("last_signal_type") == "trend_broken"
                  and t.get("reconcile") != "mainline_conflict"]
    if broken_obs:
        out_lines.append("")
        out_lines.append("## 🔻 破位观察中（趋势破坏，本轮不产候选）")
        out_lines.append("")
        for t in broken_obs:
            out_lines.append(f"- **{t['theme']}**：最后信号 trend_broken——板块跌破MA60且MA20向下，进入破位观察期（连续破位3天出池），本轮不产候选，等待趋势修复信号。")
        out_lines.append("")

    report_path = os.path.join(DATA_DIR, f"faded_watch_leaders_{date_compact}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    print(f"✅ 已生成: {report_path}", file=sys.stderr)
    # 摘要输出（cron 汇报用）
    print("")
    print(f"✅ 监控池龙头分析完成（{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}）")
    print(f"监控池主线：{len(themes)}条")
    lv_disp = {"A": "🅰️高", "B": "🅱️中", "C": "🅲️观察", "R": "✅回归"}
    for t in themes:
        lv, note, focus = theme_levels[t["theme"]]
        focus_txt = f"（仅{', '.join(focus)}）" if focus else ""
        print(f"  {lv_disp[lv]} {t['theme']}（{t.get('last_signal_type','?')}）{focus_txt}")
    print(f"A/B级深挖：{len(active_themes)}条（{'、'.join(t['theme'] for t in active_themes) or '无'}）")
    if all_kept:
        top = {}
        for tname, r in all_kept:
            if tname not in top:
                top[tname] = []
            top[tname].append(f"{r['st']['name']}({r['st']['ltsz']:.0f}亿,{r['level']})")
        for tname, names in top.items():
            print(f"交易机会：{tname} → {'、'.join(names[:2])}")
    else:
        print("交易机会：无（今日如实空仓）")
    print(f"报告路径：{report_path}")


if __name__ == "__main__":
    asyncio.run(main())
