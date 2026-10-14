#!/usr/bin/env python3
"""
refine_theme_leaders.py — 主线龙头精筛（16:45 五层漏斗）
================================================================
在 fetch_theme_leaders.py 主线维度分析基础上，对候选池做**五层精筛漏斗**，
每条覆盖主线只保留 5-8 只"可动手"标的，输出含完整交易参数的「今日可执行清单」。
输入报告无候选池时（全部主线被 16:30 技术拦截或股票池为空），输出说明性精筛报告（无清单）。

精筛维度（全部客观可观测数据，禁用推算型指标如主力资金）：
  L1 量价确认：量比（K线自算 当日/5日均量）+ 换手率（qt 字段38）+ 近3日涨停剔除 + 20日涨幅>60%软剔
  L2 买点精度（MA10 主判据）：偏离MA10≤4% 优选(贴线) / ≤8% 优选(偏开) / ≤15% 回踩区 / >15% 观望；
     偏离MA20>25% 强制乖离警示
  L3 稳定性+风控：中大市值(≥300亿)+涨得稳 —— 真龙头结构优先豁免；活跃股需稳定三条件
     （MA20斜率>0% / 20日最大回撤≤20% / 20日涨幅>0）；斜率刚转正(0~3%)且乖离MA20>15% 联动杀；
     PE<0 亏损剔除、PE>100 题材票硬杀、PE60~100 标⚠️；RPS 仅参考不设门槛
  L4 动量分档：5日涨幅>20% 加速段禁追 / 15~20% 快速上行只沿MA10低吸 / 5~15% 健康加速 / 否则回踩正常
  L5 赔率引擎：R:R=(结构量度目标-现价)/(现价-止损) < 2 一票否决；止损=贴线-5%/非贴线 max(MA10, -8%)，目标=前高/突破量度∩[3×ATR20下限,25%上限]

用法：
    python3 refine_theme_leaders.py <YYYYMMDD>

输入： data/theme_leaders_YYYYMMDD.md（fetch_theme_leaders.py 产物）+ industry_themes 表
输出： data/refine_theme_leaders_YYYYMMDD.md
"""

import asyncio
import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_sector_leaders as fsl  # get_stock_kline 等复用

DATA_DIR = "/home/agentuser/data/定时任务打包/data"
PLATE_DIR = DATA_DIR + "/plate"


# ================= 数据获取 =================

async def get_kline_volumes(code: str, limit: int = 30) -> Optional[List[dict]]:
    """获取K线（含 volume），返回 [{date, last, volume, amount}, ...]"""
    url = (f"https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get"
           f"?code={code}&ktype=day&fqtype=qfq&limit={limit}&fromDate=&toDate=")
    try:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, lambda: urllib.request.urlopen(url, timeout=10).read().decode())
        data = json.loads(raw)
        if data.get("code") == 0 and "data" in data and "nodes" in data["data"]:
            out = []
            for n in data["data"]["nodes"]:
                try:
                    out.append({
                        "date": n.get("date", ""),
                        "last": float(n["last"]),
                        "open": float(n["open"]),
                        "high": float(n["high"]),
                        "low": float(n["low"]),
                        "volume": float(n.get("volume") or 0),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            return out
    except Exception:
        pass
    return None


async def get_quote_batch(codes: List[str]) -> Dict[str, dict]:
    """批量实时行情（qt.gtimg.cn）：换手率[38]、PE[39]、量比[49]、流通市值[44]"""
    result = {}
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        url = f"https://qt.gtimg.cn/q={','.join(chunk)}"
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
                if len(fields) > 49 and fields[2]:
                    code = f"sh{fields[2]}" if line.startswith("v_sh") else f"sz{fields[2]}"
                    try:
                        result[code] = {
                            "turnover": float(fields[38]) if fields[38] else None,  # 换手率%
                            "pe": fields[39] if fields[39] else None,               # PE(TTM)
                            "vol_ratio": float(fields[49]) if fields[49] else None, # 量比
                            "ltsz": float(fields[44]) if fields[44] else None,      # 流通市值亿
                        }
                    except (ValueError, IndexError):
                        continue
        except Exception:
            pass
    return result


def parse_theme_leaders(filepath: str) -> List[dict]:
    """解析 theme_leaders_YYYYMMDD.md，返回 [{"theme": 主线名, "stocks": [...], "status": ...}, ...]"""
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    themes = []
    # 主线章节：## 🟢 主线名（状态：active）
    blocks = re.split(r"^## (🟢|🟡|🟠|⚫) (.+?)（状态：(.+?)）", content, flags=re.M)
    # blocks: [前言, emoji, name, status, 章节内容, emoji, name, status, 章节内容, ...]
    for i in range(1, len(blocks) - 3, 4):
        emoji, name, status, body = blocks[i], blocks[i + 1], blocks[i + 2], blocks[i + 3]
        stocks = []
        # 龙头验证表（只取表内行）
        table_match = re.search(r"### 龙头验证（均线照妖镜）\n(.*?)\n(?=###|\Z)", body, re.DOTALL)
        if table_match:
            for line in table_match.group(1).split("\n"):
                if "|" not in line or "股票名称" in line or ":---" in line:
                    continue
                cells = [c.strip() for c in line.split("|")]
                # | 股票名称 | 代码 | 来源 | 流通市值(亿) | 20日涨幅 | MA20斜率 | 20日回撤 | RPS_池内 | 5日涨幅 | MA10偏离 | MA20偏离 | BIAS6 | 均线结构 | 入场判定 |
                if len(cells) >= 15 and re.match(r"^(sh|sz)\d{6}$", cells[2]):
                    def _num(s):
                        try:
                            return float(s.replace("+", "").replace("%", ""))
                        except (ValueError, TypeError):
                            return None
                    stocks.append({
                        "name": cells[1], "code": cells[2], "src": cells[3],
                        "ltsz": _num(cells[4]), "d20": _num(cells[5]),
                        "ma20_slope": _num(cells[6]), "dd20": _num(cells[7]),
                        "rps": _num(cells[8]),
                        "zdf5": _num(cells[9]), "ma10_dev": _num(cells[10]), "ma20_dev": _num(cells[11]),
                        "bias6": _num(cells[12]), "structure": cells[13], "entry": cells[14],
                    })
        themes.append({"emoji": emoji, "theme": name, "status": status, "stocks": stocks})
    return [t for t in themes if t["stocks"]]  # 只保留有候选池的主线


def parse_all_theme_sections(text: str) -> List[str]:
    """解析全部主线节头，返回链名全集（不含 ### 子节）。"""
    return [match.group(1).strip()
            for match in re.finditer(r"^## (?:🟢|🟡|🟠|⚫|⚪)?\s*(.+?)（状态：.+?）", text, flags=re.M)]


def parse_dropped_themes(text: str) -> List[dict]:
    """解析“声明池>0且龙头验证表0行”的建池即全剔除主线。"""
    from collections import Counter

    result = []
    blocks = re.split(r"^## (🟢|🟡|🟠|⚫|⚪) (.+?)（状态：(.+?)）", text, flags=re.M)
    for i in range(1, len(blocks) - 3, 4):
        name, body = blocks[i + 1], blocks[i + 3]
        pool_match = re.search(r"- \*\*股票池：\*\* (\d+) 只", body)
        if not pool_match:
            continue
        declared_pool = int(pool_match.group(1))
        table_match = re.search(
            r"### 🚫 已剔除（超跌反弹/弱势）\n(.*?)(?=\n###|\n---|\Z)", body, re.DOTALL)
        reasons = []
        if table_match:
            for line in table_match.group(1).split("\n"):
                if "|" not in line or "股票名称" in line or ":---" in line:
                    continue
                cells = [cell.strip() for cell in line.split("|")]
                if len(cells) >= 8 and re.match(r"^(sh|sz)\d{6}$", cells[2]):
                    reasons.append(cells[7])
        if declared_pool > 0 and len(reasons) == declared_pool:
            result.append({
                "theme": name,
                "declared_pool": declared_pool,
                "dropped_count": len(reasons),
                "reason_counts": Counter(reasons),
            })
    return result


def parse_empty_pool_themes(filepath: str) -> List[dict]:
    """解析 theme_leaders 报告中空池主线的口径说明段（F3 §二）。

    返回 [{"theme": 主线名, "reason": 原因, "top3": Top3文本, "conflict": 冲突提示}, ...]
    """
    with open(filepath, encoding="utf-8") as f:
        content = f.read()
    result = []
    blocks = re.split(r"^## (🟢|🟡|🟠|⚫|⚪) (.+?)（状态：(.+?)）", content, flags=re.M)
    for i in range(1, len(blocks) - 3, 4):
        name, body = blocks[i + 1], blocks[i + 3]
        m = re.search(r"### ⚠️ 口径说明\n(.*?)(?=\n###|\n---|\Z)", body, re.DOTALL)
        if not m:
            continue
        section = m.group(1)
        reason_m = re.search(r"\*\*空池原因：\*\*\s*(.+)", section)
        top3_m = re.search(r"\*\*链内成分基线流通市值 Top3：\*\*\s*(.+)", section)
        conflict_m = re.search(r"\*\*口径冲突提示：\*\*\s*(.+)", section)
        result.append({
            "theme": name,
            "reason": reason_m.group(1).strip() if reason_m else "—",
            "top3": top3_m.group(1).strip() if top3_m else "—",
            "conflict": conflict_m.group(1).strip() if conflict_m else "",
        })
    return result


# ================= 精筛规则 =================

def is_limit_up_threshold(code: str) -> float:
    """涨停阈值：创业板30/科创板68 → 19.5%；其余主板 → 9.8%"""
    if code.startswith(("sz30", "sh68", "sz301")):
        return 19.5
    return 9.8


def check_last3_limit_up(klines: List[dict], code: str) -> bool:
    """近3个交易日是否有涨停（含当日）"""
    if not klines or len(klines) < 5:
        return False
    threshold = is_limit_up_threshold(code)
    for i in range(-3, 0):  # 最近3个日间涨幅
        prev, cur = klines[i - 1]["last"], klines[i]["last"]
        if prev <= 0:
            continue
        if (cur - prev) / prev * 100 >= threshold:
            return True
    return False


def moment_tier(zdf5: Optional[float]) -> Tuple[str, str]:
    """L4 动量状态分档（5日涨幅主判据，防加速段追高 + 防动能力竭）
    返回 (label, action)：action in {ok, degrade, kill}
    """
    if zdf5 is None:
        return "动量N/A", "ok"
    if zdf5 > 20:
        return f"加速段(5日{zdf5:+.0f}%)", "kill"      # 一字板/连续涨停式追赶，禁追
    if zdf5 > 15:
        return f"快速上行(5日{zdf5:+.0f}%)", "degrade"  # 只沿MA10低吸，不追高
    if zdf5 >= 5:
        return f"健康加速(5日{zdf5:+.0f}%)", "ok"       # 主升段标准形态
    if zdf5 >= 0:
        return f"温和(5日{zdf5:+.0f}%)", "ok"           # 横盘/蓄势
    return f"小幅回踩(5日{zdf5:+.0f}%)", "ok"           # 回踩正常，贴线即买点


def calc_rr(st: dict, price: Optional[float], klines: Optional[List[dict]] = None) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """L5 赔率引擎 v2（方案A）：返回 (rr, stop_px, target_px, stop_pct)
    止损：贴线票(-5% 紧保护) / 非贴线票 max(MA10价, -8%) 取更近保护位（不变）
    目标：结构量度目标 ∩ [3×ATR20下限, +25%上限]，下限≥+8%（klines<25根降级回退+15%）
    R:R < 2 → 一票否决（赚不到止损的两倍，不值得下注）
    """
    if price is None:
        return None, None, None, None
    dev10 = st["ma10_dev"]
    is_tieline = dev10 is not None and abs(dev10) <= 4.0
    if is_tieline:
        stop_pct = 0.05  # 贴线：紧止损
    else:
        stop_pct = 0.08  # 非贴线：绝对保护上限
        # 若 MA10 在现价下方且距离小于 8%，用 MA10 做更近止损（保护更多本金）
        if dev10 is not None and dev10 > 0:
            ma10_px = price / (1 + dev10 / 100)  # 由偏离反推 MA10 价格
            loss_pct_ma10 = (price - ma10_px) / price * 100
            if 0 < loss_pct_ma10 < 8.0:
                stop_pct = loss_pct_ma10 / 100
    stop_px = price * (1 - stop_pct)
    if stop_px >= price:
        return None, None, None, None

    # ---- 方案A v2：结构量度目标 ----
    # 数据不足降级：回退旧 +15% 固定目标逻辑（避免误杀）
    if klines is None or len(klines) < 25:
        target_px = price * 1.15
        rr = (target_px - price) / (price - stop_px)
        return rr, stop_px, target_px, stop_pct * 100

    # 第1步 结构目标（窗口=含当日的近3月K线）
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    prev_high = max(highs)
    gap_pct = (prev_high - price) / price * 100
    # 前高所在K线索引（多相等高点取最晚一根）
    high_idx = max(i for i, h in enumerate(highs) if h == prev_high)
    if price < prev_high and gap_pct >= 6:
        # case a: 前高即目标（尊重套牢盘压力）
        target_struct = prev_high
        if gap_pct > 12:
            # 深度回调、前高过远 → 压回 +15%
            target_struct = min(prev_high, price * 1.15)
    else:
        # case b(price<prev_high且gap<6) / case c(price>=prev_high): 量度目标
        # box_low = 前高所在K线(含)之后的 min(low)（回测锁定口径）
        box_low = min(lows[high_idx:])
        target_struct = prev_high + (prev_high - box_low)

    # 第2步 ATR20 下限（3倍20日ATR≈10-15天1.5-2个标准波幅）
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i - 1]["last"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr_window = trs[-20:] if len(trs) >= 20 else trs
    atr20 = sum(atr_window) / len(atr_window) if atr_window else 0.0
    atr_target = price * (1 + 3 * atr20 / price)

    # 第3步 周期上限（10-15天超出+25%不现实）
    target_up = price * 1.25

    # 第4步 取目标：min(结构, 上限)，再≥下限 floor
    target = min(target_struct, target_up)
    floor = max(atr_target, price * 1.08)  # 低于+8%不值得做
    if target < floor:
        target = floor

    target_px = target
    rr = (target_px - price) / (price - stop_px)
    return rr, stop_px, target_px, stop_pct * 100


def refine_stock(st: dict, klines: List[dict], quote: dict) -> dict:
    """对单只候选执行五层精筛，返回 {st, verdict, reasons, level}"""
    reasons = []
    kills = []       # 硬剔除（有任一即剔除）
    soft_kills = []  # 软剔除（数量充足时剔除，不足时补位）

    # ---- L1 量价确认 ----
    vol_ratio = None
    if klines and len(klines) >= 6:
        vols = [k["volume"] for k in klines[-6:-1]]  # 前5日
        if any(v > 0 for v in vols):
            avg5 = sum(vols) / len(vols)
            if avg5 > 0:
                vol_ratio = klines[-1]["volume"] / avg5
    today_down = False
    if klines and len(klines) >= 2:
        today_down = klines[-1]["last"] < klines[-2]["last"]
    if vol_ratio is not None:
        if today_down and vol_ratio >= 1.5:
            kills.append(f"放量下跌(量比{vol_ratio:.1f})")
        elif today_down and vol_ratio < 1.0:
            reasons.append(f"缩量回踩(量比{vol_ratio:.1f})")
        elif vol_ratio < 0.7:
            reasons.append(f"缩量(量比{vol_ratio:.1f})")
        elif vol_ratio > 2.0:
            kills.append(f"天量(量比{vol_ratio:.1f})")
        else:
            reasons.append(f"量比{vol_ratio:.1f}")
    else:
        reasons.append("量比N/A")

    # 换手率（qt 实测快照）
    turnover = quote.get("turnover") if quote else None
    if turnover is not None:
        if turnover > 20:
            kills.append(f"换手{turnover:.1f}%过高(游资化)")
        elif turnover < 3:
            reasons.append(f"低换手{turnover:.1f}%")
        else:
            reasons.append(f"换手{turnover:.1f}%")

    # 近3日涨停
    if check_last3_limit_up(klines or [], st["code"]):
        kills.append("近3日涨停(情绪票)")

    # 20日涨幅透支（软剔除：数量不足时补位保留，标记⚠️）
    if st["d20"] is not None and st["d20"] > 60:
        soft_kills.append(f"20日涨幅{st['d20']:.0f}%透支")

    # ---- L2 买点精度（双基准：MA10 主判据 + MA20 极端乖离风控 + 场景感知） ----
    # 主升浪中强势股沿 MA10 上行：偏离MA10≤4% 优选(贴线) / ≤8% 优选(偏开) / 8~15% 回踩区 / >15% 观望
    # 偏离MA20>25%（加速赶顶）强制降级观望，防止"MA10 尚可但股价已远离趋势中枢"的假回踩
    # 场景感知：同一个乖离值在不同市场场景下含义不同
    #   - 趋势启动期：BIAS_6 8~15%是正常突破乖离，不是危险信号
    #   - 高潮加速期：BIAS_6 8~15%是加速赶顶，不能追
    #   - 退潮反弹期：BIAS_6可能<10%但均线空头，反弹是卖点
    dev10 = abs(st["ma10_dev"]) if st["ma10_dev"] is not None else None
    dev20 = abs(st["ma20_dev"]) if st["ma20_dev"] is not None else None
    bias6 = st["bias6"] if st["bias6"] is not None else None
    structure = st["structure"]

    # 场景感知修正：超跌反弹结构下，即使偏离MA10很小也是反弹非反转
    if structure == "超跌反弹":
        level, dev_reason = "观望", f"超跌反弹（均线空头，反弹非反转）"
    # 高潮加速场景：BIAS_6>15% 强制降级
    elif bias6 is not None and bias6 > 15:
        level, dev_reason = "观望", f"乖离警示(BIAS_6 {bias6:.1f}%>15%，加速赶顶)"
    elif dev20 is not None and dev20 > 25:
        level, dev_reason = "观望", f"乖离警示(MA20 {st['ma20_dev']:+.1f}%>25%)"
    elif dev10 is None:
        level, dev_reason = "观望", "MA10偏离N/A"
    elif dev10 <= 4:
        level, dev_reason = "优选(贴线)", f"偏离MA10 {st['ma10_dev']:+.1f}%（贴线，最佳买点）"
    elif dev10 <= 8:
        level, dev_reason = "优选(偏开)", f"偏离MA10 {st['ma10_dev']:+.1f}%（偏开，可介入/等回踩）"
    elif dev10 <= 15:
        level, dev_reason = "回踩区", f"偏离MA10 {st['ma10_dev']:+.1f}%（等回踩MA10）"
    else:
        level, dev_reason = "观望", f"偏离MA10 {st['ma10_dev']:+.1f}%>15%"

    # ---- L3 稳定性+风控（中大市值+涨得稳，RPS 仅参考不做门槛） ----
    # 稳定性三条件（真龙头多头排列结构优先，豁免；活跃股必须全部通过）：
    #   ① MA20斜率>0% —— 中期趋势必须向上（均线向下=趋势走弱/高位回落中）
    #   ② 20日最大回撤≤20% —— 抗跌/涨得稳（防高波动追高接盘；>20%=暴涨暴跌形态）
    #   ③ 20日涨幅>0 —— 方向向上（防阴跌反抽）
    if st["structure"] == "活跃股":
        # 斜率阈值：> 0%（均线必须向上才算"涨得稳"）
        if st["ma20_slope"] is None or st["ma20_slope"] <= 0:
            slope_v = st["ma20_slope"] if st["ma20_slope"] is not None else 0.0
            kills.append(f"MA20斜率{slope_v:+.1f}%破位")
        # 斜率与乖离联动：斜率刚转正(0~3%)但乖离>15% → 均线刚开始拐头、股价已跑远（高位回落风险）
        if st["ma20_slope"] is not None and 0 < st["ma20_slope"] <= 3:
            dev20a = abs(st["ma20_dev"]) if st["ma20_dev"] is not None else None
            if dev20a is not None and dev20a > 15:
                kills.append(f"高位回落风险(斜率{st['ma20_slope']:+.1f}%+乖离{st['ma20_dev']:+.1f}%)")
        if st["dd20"] is not None and st["dd20"] > 20:
            kills.append(f"20日回撤{st['dd20']:.1f}%过大")
        if st["d20"] is not None and st["d20"] <= 0:
            kills.append(f"20日涨幅{st['d20']:.1f}%为负")
    if st["ltsz"] is not None and st["ltsz"] < 300:
        kills.append(f"市值{st['ltsz']:.0f}亿<300")

    pe = quote.get("pe") if quote else None
    pe_note = ""
    if pe is not None:
        try:
            pe_f = float(pe)
            if pe_f < 0:
                kills.append(f"亏损(PE{pe})")
                pe_note = " ⚠️亏损"
            elif pe_f > 100:
                # 估值硬杀：>100 = 纯题材定价，出不了业绩，禁入主推
                kills.append(f"PE{pe_f:.0f}题材票")
                pe_note = " ⚠️题材票"
            elif pe_f > 60:
                pe_note = " ⚠️高估值"  # 60~100 只标注不杀
        except ValueError:
            pass

    # ---- L4 动量状态（5日涨幅分档，防加速段追高 + 防动能力竭） ----
    mom_label, mom_action = moment_tier(st.get("zdf5"))
    if mom_action == "kill":
        kills.append(mom_label)
    elif mom_action == "degrade":
        reasons.append(mom_label + "(只沿MA10低吸)")

    # ---- L5 赔率引擎（R:R 一票否决 < 2） ----
    price = klines[-1]["last"] if klines else None
    rr, stop_px, target_px, stop_pct = calc_rr(st, price, klines)
    rr_note = ""
    if rr is not None:
        if rr < 2.0:
            kills.append(f"赔率不足(R:R {rr:.1f})")
            rr_note = f" ⚠️R:R{rr:.1f}"
        elif rr >= 3.0:
            reasons.append(f"R:R {rr:.1f} 优质")
            rr_note = f" R:R{rr:.1f}👍"
        else:
            reasons.append(f"R:R {rr:.1f} 合格")
            rr_note = f" R:R{rr:.1f}"

    # 软剔除标记：仅当无硬剔除时，透支作为"待补位"候选
    soft_note = ""
    if soft_kills:
        soft_note = " ⚠️" + "、".join(soft_kills)

    verdict = "❌ 剔除" if kills else "✅ 保留"
    return {
        "st": st, "verdict": verdict, "kills": kills, "reasons": reasons,
        "soft_kills": soft_kills, "soft_note": soft_note,
        "level": level, "dev_reason": dev_reason, "pe_note": pe_note,
        "vol_ratio": vol_ratio, "turnover": turnover, "pe": pe,
        "mom_label": mom_label, "rr": rr, "stop_px": stop_px,
        "target_px": target_px, "stop_pct": stop_pct, "rr_note": rr_note,
    }


# ================= 渲染 =================

def render_candidates(theme, refined: List[dict], kept: List[dict]) -> str:
    out = []
    degrade = " ⚠️候选不足，降级观察" if len(kept) < 5 else ""
    out.append(f"### {theme['emoji']} {theme['theme']}（精筛后 {len(kept)} 只{degrade}）")
    out.append("")
    # 场景匹配段
    scene = identify_market_scene([r["st"] for r in refined], theme["theme"] or "")
    scene_emoji = {"趋势启动": "🚀", "趋势中继": "📈", "高潮加速": "⚡", "退潮反弹": "🔄", "混沌期": "🌫️"}.get(scene["scene"], "❓")
    out.append(f"> **市场场景：{scene_emoji} {scene['scene']}** — {scene['action']}（止损：{scene['stop']}）")
    out.append(">")
    for b in scene["basis"]:
        out.append(f"> - {b}")
    out.append(">")
    out.append(f"> **场景化追高判断：** 当前为{scene['scene']}期，乖离含义见上表")
    out.append("")
    out.append("| 股票名称 | 代码 | 来源 | 流通市值(亿) | MA20斜率 | 20日回撤 | RPS_池内 | 距MA10 | 距MA20 | 量比 | 换手率 | PE | 均线结构 | 动量 | 精筛判定 | 止损 | 目标 | R:R |")
    out.append("|:--------|:---:|:----|:----------:|:-----:|:-----:|:-----:|:----:|:----:|:----:|:----:|:---:|:----|:----|:----|:----:|:----:|:----:|")
    for r in kept:
        st = r["st"]
        q_vol = f"{r['vol_ratio']:.2f}" if r["vol_ratio"] is not None else "N/A"
        q_tur = f"{r['turnover']:.1f}%" if r["turnover"] is not None else "N/A"
        q_pe = r["pe"] if r["pe"] else "N/A"
        rps_s = f"{st['rps']:.0f}" if st["rps"] is not None else "N/A"
        slope_s = f"{st['ma20_slope']:+.1f}%" if st["ma20_slope"] is not None else "N/A"
        dd_s = f"{st['dd20']:.1f}%" if st["dd20"] is not None else "N/A"
        dev10_s = f"{st['ma10_dev']:+.1f}%" if st["ma10_dev"] is not None else "N/A"
        dev20_s = f"{st['ma20_dev']:+.1f}%" if st["ma20_dev"] is not None else "N/A"
        rr_s = f"{r['rr']:.2f}" if r["rr"] is not None else "N/A"
        stop_s = f"{r['stop_px']:.2f}(-{r['stop_pct']:.0f}%)" if r["stop_px"] is not None else "N/A"
        tgt_s = f"{r['target_px']:.2f}" if r["target_px"] is not None else "N/A"
        out.append(
            f"| {st['name']} | {st['code']} | {st['src']} | {st['ltsz']:.0f} | {slope_s} | {dd_s} | {rps_s} | {dev10_s} | {dev20_s} | "
            f"{q_vol} | {q_tur} | {q_pe} | {st['structure']} | {r['mom_label']} | {r['level']}{r['pe_note']} | {stop_s} | {tgt_s} | {rr_s} |"
        )
    out.append("")
    # 一句话依据
    for r in kept:
        st = r["st"]
        basis = "，".join(r["reasons"] + [r["dev_reason"]])
        rr_txt = f"，止损{r['stop_px']:.2f} 目标{r['target_px']:.2f} R:R {r['rr']:.1f}" if r["stop_px"] is not None else ""
        out.append(f"- **{st['name']}**：{st['structure']} {r['level']}（{basis}{rr_txt}）")
    out.append("")
    # 剔除摘要
    killed = [r for r in refined if r["verdict"] == "❌ 剔除"]
    if killed:
        from collections import Counter
        cnt = Counter()
        for r in killed:
            for k in r["kills"]:
                cnt[k] += 1
        summary = "；".join(f"{k}({v}只)" for k, v in cnt.most_common(8))
        out.append(f"> 🚫 剔除 {len(killed)} 只：{summary}")
        out.append("")
    return "\n".join(out)


# ================= 场景识别（市场场景匹配） =================

def identify_market_scene(results: List[dict], theme_name: str) -> dict:
    """
    基于龙头股均线结构、乖离、量能识别当前市场场景。

    返回：{"scene", "basis", "action", "stop"}
    """
    if not results:
        return {"scene": "混沌期", "basis": ["无候选数据"], "action": "观望", "stop": "-"}

    true_leaders = [r for r in results if r.get("structure") == "真龙头"]
    active_stocks = [r for r in results if r.get("structure") == "活跃股"]

    dev10s = [abs(r.get("ma10_dev") or 0) for r in results if r.get("ma10_dev") is not None]
    dev20s = [abs(r.get("ma20_dev") or 0) for r in results if r.get("ma20_dev") is not None]
    avg_dev10 = sum(dev10s) / len(dev10s) if dev10s else None
    avg_dev20 = sum(dev20s) / len(dev20s) if dev20s else None

    high_bias = [r for r in results if r.get("bias6") is not None and r["bias6"] > 15]
    high_bias_ratio = len(high_bias) / len(results) if results else 0

    oversold = [r for r in results if r.get("structure") == "超跌反弹"]
    oversold_ratio = len(oversold) / len(results) if results else 0

    slopes = [r.get("ma20_slope") or 0 for r in results if r.get("ma20_slope") is not None]
    avg_slope = sum(slopes) / len(slopes) if slopes else None

    basis = []

    if high_bias_ratio > 0.3 and avg_slope is not None and avg_slope > 2:
        basis.append(f"高乖离占比{high_bias_ratio:.0%}（BIAS_6>15%的股票较多）")
        basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势陡峭）")
        if avg_dev10 is not None and avg_dev10 > 10:
            basis.append(f"平均偏离MA10{avg_dev10:.1f}%（乖离过大）")
        return {"scene": "高潮加速", "basis": basis, "action": "减仓", "stop": "-"}

    if true_leaders and avg_dev10 is not None and 3 <= avg_dev10 <= 10:
        basis.append(f"真龙头{len(true_leaders)}只，活跃股{len(active_stocks)}只")
        basis.append(f"平均偏离MA10{avg_dev10:.1f}%（均值回归区域）")
        if avg_slope is not None and avg_slope > 0:
            basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势向上）")
        return {"scene": "趋势中继", "basis": basis, "action": "加仓", "stop": "MA20"}

    if true_leaders and avg_dev10 is not None and avg_dev10 > 10:
        basis.append(f"真龙头{len(true_leaders)}只（刚突破/加速中）")
        basis.append(f"平均偏离MA10{avg_dev10:.1f}%（突破乖离）")
        if avg_slope is not None and avg_slope > 0:
            basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势向上）")
        return {"scene": "趋势启动", "basis": basis, "action": "首仓介入", "stop": "MA20下方2%"}

    if oversold_ratio > 0.5:
        basis.append(f"超跌反弹占比{oversold_ratio:.0%}（大部分在年线下方反弹）")
        if avg_slope is not None:
            basis.append(f"平均MA20斜率{avg_slope:+.1f}%（趋势偏弱）")
        return {"scene": "退潮反弹", "basis": basis, "action": "不参与", "stop": "-"}

    if not true_leaders:
        basis.append(f"无真龙头（全部为活跃股/超跌反弹）")
        if oversold_ratio > 0.3:
            basis.append(f"超跌反弹占比{oversold_ratio:.0%}")
        if avg_dev10 is not None:
            basis.append(f"平均偏离MA10{avg_dev10:.1f}%（方向不明）")
        return {"scene": "混沌期", "basis": basis, "action": "观望", "stop": "-"}

    basis.append(f"真龙头{len(true_leaders)}只/活跃股{len(active_stocks)}只/超跌反弹{len(oversold)}只")
    return {"scene": "混沌期", "basis": basis, "action": "观望", "stop": "-"}


# 全局缓存（渲染用）
quote_cache: Dict[str, dict] = {}


def process_watch_pool(date_db: str, a_list: list, b_list: list) -> int:
    """精筛 A/B级保留标的幂等写入 portfolio.watch_pool 观察池表。

    幂等键 (stock_code, pool_date) —— 同日重跑 ON DUPLICATE KEY UPDATE 覆盖不新增。
    返回写入行数；落库失败打印警告到 stderr，不抛异常不阻断主流程。
    """
    conn = None
    count = 0
    try:
        import pymysql
        conn = pymysql.connect(host="127.0.0.1", port=3306, user="wish",
                               password="<DB_PASSWORD>", database="portfolio",
                               charset="utf8mb4", autocommit=False)
        cur = conn.cursor()
        sql = (
            "INSERT INTO watch_pool "
            "(stock_code, stock_name, theme_name, pool_date, grade, level, structure, "
            " ltsz, entry_start, entry_end, stop_px, target_px, rr, position_hint, "
            " scene_name, trigger_condition, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'watching') "
            "ON DUPLICATE KEY UPDATE "
            " stock_name=VALUES(stock_name), theme_name=VALUES(theme_name), grade=VALUES(grade),"
            " level=VALUES(level), structure=VALUES(structure), ltsz=VALUES(ltsz),"
            " entry_start=VALUES(entry_start), entry_end=VALUES(entry_end),"
            " stop_px=VALUES(stop_px), target_px=VALUES(target_px), rr=VALUES(rr),"
            " position_hint=VALUES(position_hint), scene_name=VALUES(scene_name),"
            " trigger_condition=VALUES(trigger_condition)"
        )
        # A级条目：(theme, r, stop_s, tgt_s)
        for theme, r, stop_s, tgt_s in a_list:
            st = r["st"]
            price = r["stop_px"] * 100 / (100 - r["stop_pct"]) if r["stop_px"] and r["stop_pct"] else None
            entry_start = round(price * 0.99, 2) if price else None
            entry_end = round(price * 1.01, 2) if price else None
            pos = "≤10%" if st["structure"] == "真龙头" else "≤8%"
            cur.execute(sql, (
                st["code"], st["name"], theme, date_db, "A级", r["level"],
                st["structure"], st["ltsz"], entry_start, entry_end,
                r["stop_px"], r["target_px"], r["rr"], pos,
                None, None,
            ))
            count += 1
        # B级条目：(theme, r, stop_s, tgt_s, scene_name)
        for theme, r, stop_s, tgt_s, scene_name in b_list:
            st = r["st"]
            if r["level"] in ("优选(贴线)", "优选(偏开)"):
                cond = f"动量待确认（当前{r['mom_label']}）"
            elif r["level"] == "回踩区":
                cond = f"等回踩MA10（现偏离{st['ma10_dev']:+.1f}%）"
            else:
                cond = f"等买点{st['ma10_dev']:+.1f}%逼近MA10／动量确认"
            if scene_name in ("混沌期", "退潮反弹"):
                cond += f"；🌫️ 场景观望降级（{scene_name}禁A级）"
            cur.execute(sql, (
                st["code"], st["name"], theme, date_db, "B级", r["level"],
                st["structure"], st["ltsz"], None, None,
                r["stop_px"], r["target_px"], r["rr"], None,
                scene_name, cond,
            ))
            count += 1
        conn.commit()
    except Exception as e:
        print(f"⚠️ 观察池落库失败: {e}", file=sys.stderr)
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
    return count


async def main():
    if len(sys.argv) < 2:
        print("用法: python3 refine_theme_leaders.py <YYYYMMDD>", file=sys.stderr)
        sys.exit(1)
    date_compact = sys.argv[1]
    date_db = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    # 生成日期：与当天一致则为自动执行，否则标注补跑日期
    today = datetime.now().strftime("%Y%m%d")
    run_label = "自动执行" if date_compact == today else f"手动补跑 {date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
    src_file = os.path.join(DATA_DIR, f"theme_leaders_{date_compact}.md")
    if not os.path.exists(src_file):
        print(f"❌ 输入文件不存在（先运行 fetch_theme_leaders.py）: {src_file}", file=sys.stderr)
        sys.exit(2)
    with open(src_file, encoding="utf-8") as f:
        source_text = f.read()

    themes = parse_theme_leaders(src_file)
    if not themes:
        # 报告存在但无候选池（如全部 active 主线被技术破位拦截或股票池为空）
        # → 输出说明性精筛报告（rc=0），不算失败；只有报告为空才视为上游异常
        if os.path.getsize(src_file) == 0:
            print("❌ theme_leaders 报告为空（fetch_theme_leaders.py 未产出内容）", file=sys.stderr)
            sys.exit(3)
        print(f"# 🔬 主线龙头精筛 {date_compact}")
        print("")
        print(f"> 生成时间：{run_label}")
        print(f"> 输入：theme_leaders_{date_compact}.md（无 active 且未破位主线候选池）")
        print(f"> 精筛口径：五层漏斗 —— L1量价(量比/换手/涨停/透支) → L2买点精度(MA10偏离≤4%贴线/≤8%偏开优选/≤15%回踩，MA20>25%乖离警示) → L3稳定性风控(中大市值≥300亿+涨得稳：MA20斜率>0%/20日回撤≤20%/20日涨幅>0，斜率刚转正+乖离>15%联动杀，亏损剔除，真龙头豁免，RPS仅参考) → L4动量分档(5日涨幅：>20%加速段禁追/>15%快速上行低吸/5~15%健康加速/回踩正常)+估值从紧(PE>100题材票硬杀，60~100标⚠️) → L5赔率引擎(R:R=(结构量度目标-现价)/(现价-止损)≥2一票否决；止损=贴线-5%/非贴线MA10或-8%，目标=前高/突破量度∩[3×ATR20下限,25%上限]，深度回调gap>12%压回+15%)")
        print("")
        print("---")
        print("")
        print("**⏭️ 当日无候选池：** 全部 active 主线均被 16:30 技术确认拦截（break_level≥2）或无候选股票，精筛无对象，无可执行清单。")
        print("")
        print(f"> 参考：{src_file} 头部已列出技术破位降级明细与当日主线全景。")
        print("")
        empty_pool = parse_empty_pool_themes(src_file)
        if empty_pool:
            print("### ⚠️ 空池主线与口径说明")
            print("")
            for e in empty_pool:
                print(f"- **{e['theme']}**：{e['reason']}")
                print(f"  - 成分最大市值 Top3：{e['top3']}")
            if e["conflict"]:
                print(f"  - {e['conflict']}")
        print("")
        print("- 口径说明：精筛仅对建池成功且存在非剔除标的的主线产候选；建池即全剔除主线属已分析、无合格标的，非未分析。")
        print("")
        dropped_pool = parse_dropped_themes(source_text)
        if dropped_pool:
            print(f"### ⚠️ 建池即全剔除（{len(dropped_pool)} 条）")
            print("")
            for dropped in dropped_pool:
                reason_text = "；".join(f"{reason}({count})" for reason, count in dropped["reason_counts"].most_common())
                print(f"- **{dropped['theme']}**：声明池 {dropped['declared_pool']} 只 → 全部被剔除（{dropped['dropped_count']} 只）")
                print(f"  - 剔除原因：{reason_text}")
            print("")
        all_theme_sections = parse_all_theme_sections(source_text)
        disclosed_count = len(themes) + len(empty_pool) + len(dropped_pool)
        if disclosed_count != len(all_theme_sections):
            print(f"⚠️ 链数自校验失败: {disclosed_count} != {len(all_theme_sections)}", file=sys.stderr)
        sys.exit(0)

    print(f"# 🔬 主线龙头精筛 {date_compact}")
    print("")
    print(f"> 生成时间：{run_label}")
    print(f"> 输入：theme_leaders_{date_compact}.md 龙头验证表候选池（{sum(len(t['stocks']) for t in themes)} 只）")
    print(f"> 精筛口径：五层漏斗 —— L1量价(量比/换手/涨停/透支) → L2买点精度(MA10偏离≤4%贴线/≤8%偏开优选/≤15%回踩，MA20>25%乖离警示) → L3稳定性风控(中大市值≥300亿+涨得稳：MA20斜率>0%/20日回撤≤20%/20日涨幅>0，斜率刚转正+乖离>15%联动杀，亏损剔除，真龙头豁免，RPS仅参考) → L4动量分档(5日涨幅：>20%加速段禁追/>15%快速上行低吸/5~15%健康加速/回踩正常)+估值从紧(PE>100题材票硬杀，60~100标⚠️) → L5赔率引擎(R:R=(结构量度目标-现价)/(现价-止损)≥2一票否决；止损=贴线-5%/非贴线MA10或-8%，目标=前高/突破量度∩[3×ATR20下限,25%上限]，深度回调gap>12%压回+15%)")
    print(f"> 输出：每条 active 主线保留 5-8 只（**禁用主力资金等推算型指标，全部客观可观测数据**）")
    print("")
    print("---")
    print("")

    # 收集全部候选代码 → 批量行情
    all_codes = [st["code"] for t in themes for st in t["stocks"]]
    global quote_cache
    quote_cache = await get_quote_batch(all_codes)

    all_kept = []
    for t in themes:
        print(f"⏳ 精筛主线: {t['theme']}（候选 {len(t['stocks'])} 只）...", file=sys.stderr)
        refined = []
        for st in t["stocks"]:
            klines = await get_kline_volumes(st["code"], limit=65)
            quote = quote_cache.get(st["code"], {})
            r = refine_stock(st, klines, quote)
            refined.append(r)
        # 排序：优选(贴线) > 优选(偏开) > 回踩区 > 观望，同级按流通市值降序（中大市值优先，涨得稳优先）
        def sort_key(r):
            lvl = {"优选(贴线)": 0, "优选(偏开)": 1, "回踩区": 2, "观望": 3}.get(r["level"], 4)
            return (lvl, -(r["st"]["ltsz"] if r["st"]["ltsz"] is not None else -1))
        refined.sort(key=sort_key)
        kept = [r for r in refined if r["verdict"] == "✅ 保留"]
        # 若不足 5 只：从"无硬杀但软剔(20日透支)"候选中按市值补足；超 8 只截断
        if len(kept) < 5:
            backups = [r for r in refined
                       if r["verdict"] == "❌ 剔除" and not r["kills"] and r["soft_kills"]]
            backups.sort(key=lambda r: -(r["st"]["ltsz"] if r["st"]["ltsz"] is not None else -1))
            need = 5 - len(kept)
            for r in backups[:need]:
                r["verdict"] = "✅ 保留"
                r["soft_note"] = r["soft_note"]  # 保持⚠️标记
                kept.append(r)
            if len(kept) < 5:
                print(f"> ⚠️ {t['theme']} 硬杀后不足5只，补位后仅 {len(kept)} 只（候选池小）", file=sys.stderr)
        # 同来源板块去重：同一主线内，相同 src（如板块[工业金属]）最多保留2只，防单beta重复暴露
        # 注意：按市值排序取组内前2（保留大市值龙头），而非按当前列表顺序截断（避免紫金矿被江铜/铜陵挤出）
        from collections import defaultdict
        src_groups: Dict[str, List[dict]] = defaultdict(list)
        for r in kept:
            src_groups[r["st"]["src"]].append(r)
        tmp_kept = []
        dedup_dropped = 0
        for src, items in src_groups.items():
            # 组内排序：真龙头优先 → 买点级别(贴线>偏开>回踩>观望) → 市值降序
            # 避免"观望票(偏离大)顶掉可介入票"，也避免活跃贴线票挤掉真龙头
            lvl_order = {"优选(贴线)": 0, "优选(偏开)": 1, "回踩区": 2, "观望": 3, "乖离警示": 4}
            items.sort(key=lambda r: (
                0 if r["st"]["structure"] == "真龙头" else 1,
                lvl_order.get(r["level"], 4),
                -(r["st"]["ltsz"] if r["st"]["ltsz"] is not None else -1),
            ))
            dedup_dropped += max(0, len(items) - 2)
            tmp_kept.extend(items[:2])
        if dedup_dropped:
            print(f"> 🔁 同源去重 {dedup_dropped} 只（同一来源板块按市值保留Top2）", file=sys.stderr)
        # 恢复主线内排序（贴线>偏开>回踩，同级市值降序）
        tmp_kept.sort(key=sort_key)
        kept = tmp_kept[:8]
        # 市场场景参与 A/B 分流 —— 混沌期/退潮反弹 → 该主线保留股整体禁 A级（只降 B级）
        scene = identify_market_scene([r["st"] for r in refined], t["theme"] or "")
        scene_name = scene["scene"]
        if scene_name in ("混沌期", "退潮反弹"):
            print(f"> 🌫️ 场景={scene_name}，{t['theme']} 保留股整体降 B级（禁 A级，场景观望降级）", file=sys.stderr)
        all_kept.extend((t["theme"], r, scene_name) for r in kept)
        print(render_candidates(t, refined, kept))
        print("---")
        print("")

    # 汇总
    print("# 📌 精筛汇总（全部主线保留标的）")
    print("")
    if all_kept:
        # 统计各主线保留数量，不足5只标记降级观察
        from collections import Counter
        theme_cnt = Counter(th for th, _, _ in all_kept)
        print("| 主线 | 股票名称 | 代码 | 市值(亿) | MA20斜率 | 20日回撤 | RPS | 距MA10 | 动量 | 精筛判定 | 止损 | 目标 | R:R | 备注 |")
        print("|:----|:--------|:---:|:------:|:-----:|:-----:|:---:|:----:|:----|:----|:----:|:----:|:----:|:----|")
        for theme, r, scene_name in all_kept:
            st = r["st"]
            rps_s = f"{st['rps']:.0f}" if st["rps"] is not None else "N/A"
            slope_s = f"{st['ma20_slope']:+.1f}%" if st["ma20_slope"] is not None else "N/A"
            dd_s = f"{st['dd20']:.1f}%" if st["dd20"] is not None else "N/A"
            dev_s = f"{st['ma10_dev']:+.1f}%" if st["ma10_dev"] is not None else "N/A"
            rr_s = f"{r['rr']:.2f}" if r["rr"] is not None else "N/A"
            stop_s = f"{r['stop_px']:.2f}" if r["stop_px"] is not None else "N/A"
            tgt_s = f"{r['target_px']:.2f}" if r["target_px"] is not None else "N/A"
            note = "⚠️降级观察" if theme_cnt[theme] < 5 else ""
            if scene_name in ("混沌期", "退潮反弹"):
                note = (note + "；" if note else "") + "🌫️ 场景观望降级（禁A级）"
            print(f"| {theme} | {st['name']} | {st['code']} | {st['ltsz']:.0f} | {slope_s} | {dd_s} | {rps_s} | {dev_s} | {r['mom_label']} | {r['level']}{r['pe_note']} | {stop_s} | {tgt_s} | {rr_s} | {note} |")
        print("")
        print("**精筛逻辑：** 选股原则=中大市值(≥300亿)+涨得稳 —— 结构优先（真龙头不限条件，活跃股需稳定三条件：MA20斜率>0%/20日回撤≤20%/20日涨幅>0，斜率刚转正+乖离>15%联动杀）+ 亏损剔除（PE<0直接出局）+ 估值从紧（PE>100题材票硬杀，60~100标⚠️）+ 买点优先（偏离MA10≤4%贴线/≤8%偏开优选/≤15%回踩，偏离MA20>25%乖离警示）+ 动量分档（5日>20%加速段禁追/15~20%只沿MA10低吸）+ 赔率引擎（R:R≥2一票否决，止损=贴线-5%/非贴线MA10或-8%，目标=结构量度）+ 量价确认（缩量回踩健康，放量下跌/天量/涨停/透支剔除）+ 同源去重（同一来源板块最多2只，防单beta重复暴露）；RPS 仅参考不设门槛")
        print("")
    else:
        print("无可保留标的。")
    print("")
    # F3 §二：空池主线口径说明（精筛汇总下方单列）
    empty_pool = parse_empty_pool_themes(src_file)
    if empty_pool:
        print("### ⚠️ 空池主线与口径说明")
        print("")
        for e in empty_pool:
            print(f"- **{e['theme']}**：{e['reason']}")
            print(f"  - 成分最大市值 Top3：{e['top3']}")
            if e["conflict"]:
                print(f"  - {e['conflict']}")
        print("")
    print("- 口径说明：精筛仅对建池成功且存在非剔除标的的主线产候选；建池即全剔除主线属已分析、无合格标的，非未分析。")
    print("")
    dropped_pool = parse_dropped_themes(source_text)
    if dropped_pool:
        print(f"### ⚠️ 建池即全剔除（{len(dropped_pool)} 条）")
        print("")
        for dropped in dropped_pool:
            reason_text = "；".join(f"{reason}({count})" for reason, count in dropped["reason_counts"].most_common())
            print(f"- **{dropped['theme']}**：声明池 {dropped['declared_pool']} 只 → 全部被剔除（{dropped['dropped_count']} 只）")
            print(f"  - 剔除原因：{reason_text}")
        print("")
    all_theme_sections = parse_all_theme_sections(source_text)
    disclosed_count = len(themes) + len(empty_pool) + len(dropped_pool)
    if disclosed_count != len(all_theme_sections):
        print(f"⚠️ 链数自校验失败: {disclosed_count} != {len(all_theme_sections)}", file=sys.stderr)
    print("# 🎯 今日可执行清单（含完整交易参数）")
    print("")
    print("> 分级规则：**A级**=五层全过+优选买点+动量健康 → 现在可执行；**B级**=保留但买点/动量待触发 → 等条件；**C级**=硬剔原因见上。")
    print("")
    a_list, b_list = [], []
    for theme, r, scene_name in all_kept:
        st = r["st"]
        rr = r["rr"] if r["rr"] is not None else 0
        stop_s = f"{r['stop_px']:.2f}(-{r['stop_pct']:.0f}%)" if r["stop_px"] is not None else "N/A"
        tgt_s = f"{r['target_px']:.2f}" if r["target_px"] is not None else "N/A"
        nom_buy = r["level"] in ("优选(贴线)", "优选(偏开)")
        mom_ok = r["mom_label"] and "加速段" not in r["mom_label"]
        # 混沌期/退潮反弹 → 整体禁 A级，只降 B级（场景观望降级）
        scene_degrade = scene_name in ("混沌期", "退潮反弹")
        if nom_buy and mom_ok and rr >= 2.0 and not scene_degrade:
            a_list.append((theme, r, stop_s, tgt_s))
        else:
            b_list.append((theme, r, stop_s, tgt_s, scene_name))
    if not a_list and not b_list:
        print("无保留标的，今日无可执行清单。")
    if a_list:
        print("### ✅ A级 · 现在可执行（全条件通过）")
        print("")
        print("| 主线 | 股票 | 代码 | 市值(亿) | 入场区间(现价±1%) | 止损 | 目标 | R:R | 建议仓位 |")
        print("|:----|:----|:---:|:------:|:----|:----:|:----:|:----:|:----|")
        for theme, r, stop_s, tgt_s in a_list:
            st = r["st"]
            price = r["stop_px"] * 100 / (100 - r["stop_pct"]) if r["stop_px"] and r["stop_pct"] else None
            entry_s = f"{price*0.99:.2f}~{price*1.01:.2f}" if price else "N/A"
            pos = "≤10%" if st["structure"] == "真龙头" else "≤8%"
            print(f"| {theme} | **{st['name']}** | {st['code']} | {st['ltsz']:.0f} | {entry_s} | {stop_s} | {tgt_s} | {r['rr']:.2f} | {pos} |")
        print("")
    if b_list:
        print("### ⏳ B级 · 等触发（保留但条件未到）")
        print("")
        print("| 主线 | 股票 | 代码 | 市值(亿) | 评级 | 动量 | 待触发条件 |")
        print("|:----|:----|:---:|:------:|:----|:----|:----|")
        for theme, r, stop_s, tgt_s, scene_name in b_list:
            st = r["st"]
            if r["level"] in ("优选(贴线)", "优选(偏开)"):
                cond = f"动量待确认（当前{r['mom_label']}）"
            elif r["level"] == "回踩区":
                cond = f"等回踩MA10（现偏离{st['ma10_dev']:+.1f}%）"
            else:
                cond = f"等买点{st['ma10_dev']:+.1f}%逼近MA10／动量确认"
            if scene_name in ("混沌期", "退潮反弹"):
                cond += f"；🌫️ 场景观望降级（{scene_name}禁A级）"
            print(f"| {theme} | {st['name']} | {st['code']} | {st['ltsz']:.0f} | {r['level']} | {r['mom_label']} | {cond} |")
        print("")
    print("**风险提醒：** 目标=结构量度(10~15天波段取向)；跌破止损位无条件离场；单票仓位建议 ≤8%(活跃股)/≤10%(真龙头)。")
    # 观察池落库（幂等写入 A/B级保留标的）
    try:
        n = process_watch_pool(date_db, a_list, b_list)
        if n:
            print(f"[观察池] 已写入 {n} 只精筛标的 → portfolio.watch_pool", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ 观察池落库失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
