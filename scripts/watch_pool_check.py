#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
观察池盘中开仓条件检查（11:30 / 14:00 定时任务）
================================================
读取 portfolio.watch_pool 中 status='watching' 的观察标的（每只股票取最新 pool_date 视角），
拉取腾讯实时行情 + K线，按开仓条件判定；符合条件者推送企微 webhook（markdown），
并将该条记录 status 更新为 'triggered'（防同日 11:30/14:00 重复推送；次日精筛新记录自动回 watching）。

开仓条件（口径与 refine_theme_leaders.py / sector-leader-analysis skill 一致）：
  A级：现价 ∈ [entry_start, entry_end]（入场区间）且 现价 > stop_px（未破止损）
  B级：trigger_condition 不含"🌫️ 场景观望降级"（混沌期/退潮反弹禁A级不推）
       且满足 trigger 文本描述条件：
         - 含"等回踩MA10"      → 现价偏离MA10 ≤ 4%（贴线）
         - 含"动量待确认"      → 5日涨幅 > 0 且 ≤ 20%（动量转健康，非加速段）
         - 其他               → 现价偏离MA10 ≤ 4% 且 5日涨幅 > 0
  （破位保护：任何级别 现价 ≤ stop_px 一律不推）

推送：统一企微 webhook（msgtype=markdown）；无触发不推送（静默）。
用法：python3 watch_pool_check.py [--dry-run]
"""
import sys
import json
import time
import urllib.request
import urllib.parse
import pymysql

WEBHOOK_URL = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
               "?key=<WECOM_WEBHOOK_KEY>")
DRY_RUN = "--dry-run" in sys.argv


# ================= 数据获取 =================

def get_quote_batch(codes):
    """批量实时行情（腾讯 qt.gtimg.cn）：现价[3]、昨收[4]、换手率[38]、流通市值[44]、量比[49]"""
    result = {}
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        url = f"https://qt.gtimg.cn/q={','.join(chunk)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="ignore")
            for line in raw.split("\n"):
                line = line.strip()
                if "=" not in line or not line.startswith("v_"):
                    continue
                payload = line.split("=", 1)[1].strip().strip('"')
                fields = payload.split("~")
                if len(fields) > 49 and fields[2]:
                    code = f"sh{fields[2]}" if line.startswith("v_sh") else f"sz{fields[2]}"
                    def _f(idx):
                        try:
                            return float(fields[idx]) if fields[idx] else None
                        except (ValueError, IndexError):
                            return None
                    result[code] = {
                        "price": _f(3),        # 现价
                        "pre_close": _f(4),    # 昨收
                        "turnover": _f(38),    # 换手率%
                        "ltsz": _f(44),        # 流通市值亿
                        "vol_ratio": _f(49),   # 量比
                    }
        except Exception as e:
            print(f"[warn] 行情拉取失败 {chunk[:5]}...: {e}", file=sys.stderr)
    return result


def get_kline(code, limit=30):
    """腾讯前复权日K，返回 [{date, last}, ...] 按日期升序"""
    url = (f"https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get"
           f"?code={code}&ktype=day&fqtype=qfq&limit={limit}&fromDate=&toDate=")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode()
        data = json.loads(raw)
        out = []
        if data.get("code") == 0 and "data" in data and "nodes" in data["data"]:
            for n in data["data"]["nodes"]:
                try:
                    out.append({"date": n.get("date", ""), "last": float(n["last"])})
                except (KeyError, ValueError, TypeError):
                    continue
        return out
    except Exception as e:
        print(f"[warn] K线拉取失败 {code}: {e}", file=sys.stderr)
        return []


def calc_ma10(kline):
    """最近10个交易日收盘均值（含当日实时价，最后一根为当日）"""
    closes = [k["last"] for k in kline]
    if len(closes) < 10:
        return None
    return sum(closes[-10:]) / 10.0


def calc_chg5(kline, price):
    """5日涨幅 %：现价相对5个交易日前收盘"""
    closes = [k["last"] for k in kline]
    if len(closes) < 6:
        return None
    base = closes[-6]
    if not base:
        return None
    return (price - base) / base * 100.0


# ================= 数据库 =================

def load_watch_pool():
    """读取 status='watching' 的观察标的，按股票取最新 pool_date 视角"""
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="wish", password="<DB_PASSWORD>",
                           database="portfolio", charset="utf8mb4")
    cur = conn.cursor(pymysql.cursors.DictCursor)
    cur.execute("""
        SELECT stock_code, stock_name, theme_name, pool_date, grade, level, structure,
               ltsz, entry_start, entry_end, stop_px, target_px, rr, position_hint,
               scene_name, trigger_condition, status
        FROM watch_pool
        WHERE status = 'watching'
        ORDER BY stock_code, pool_date
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    # 同一股票取最新 pool_date 一条
    latest = {}
    for r in rows:
        key = r["stock_code"]
        if key not in latest or r["pool_date"] > latest[key]["pool_date"]:
            latest[key] = r
    return list(latest.values())


def mark_triggered(stock_code, pool_date):
    """触发推送后置 status='triggered'，防同日重复推送"""
    conn = pymysql.connect(host="127.0.0.1", port=3306, user="wish", password="<DB_PASSWORD>",
                           database="portfolio", charset="utf8mb4")
    cur = conn.cursor()
    cur.execute("UPDATE watch_pool SET status='triggered' WHERE stock_code=%s AND pool_date=%s",
                (stock_code, pool_date))
    conn.commit()
    cur.close()
    conn.close()


# ================= 判定 =================

def judge(stock, quote, kline):
    """返回 (ok, reason)；ok=True 表示符合开仓条件"""
    price = quote.get("price") if quote else None
    if price is None or price <= 0:
        return False, f"现价缺失（{quote}）"
    stop_px = stock["stop_px"]
    if stop_px and price <= stop_px:
        return False, f"已破止损（现价{price:.2f} ≤ 止损{stop_px:.2f}）"

    grade = stock["grade"]
    if grade == "A级":
        es, ee = stock["entry_start"], stock["entry_end"]
        if es and ee and es <= price <= ee:
            return True, f"现价{price:.2f}进入入场区间[{es:.2f}~{ee:.2f}]"
        if es and price < es:
            return False, f"现价{price:.2f}低于入场区间下沿{es:.2f}（未到/已跌出买点）"
        if ee and price > ee:
            return False, f"现价{price:.2f}高于入场区间上沿{ee:.2f}（追高不推）"
        return False, "入场参数缺失"

    # B级：按 trigger_condition 描述判定
    trig = stock["trigger_condition"] or ""
    if "场景观望降级" in trig:
        return False, f"场景观望降级（B级不推）：{trig[:30]}"
    ma10 = calc_ma10(kline) if len(kline) >= 10 else None
    chg5 = calc_chg5(kline, price)
    dev10 = (price - ma10) / ma10 * 100.0 if ma10 else None

    cond_parts = []
    if "等回踩MA10" in trig:
        if dev10 is None:
            return False, "K线不足无法算MA10"
        ok = dev10 <= 4.0
        reason = f"偏离MA10 {dev10:+.1f}%（{'≤4% 贴线达标' if ok else '>4% 未回踩到位'}）"
    elif "动量待确认" in trig:
        if chg5 is None:
            return False, "K线不足无法算5日动量"
        ok = 0 < chg5 <= 20.0
        reason = f"5日涨幅 {chg5:+.1f}%（{'动量转健康' if ok else '动量未确认/加速段'}）"
    else:
        if dev10 is None or chg5 is None:
            return False, "K线不足"
        ok = dev10 <= 4.0 and 0 < chg5 <= 20.0
        reason = f"偏离MA10 {dev10:+.1f}%（≤4%）且5日涨幅 {chg5:+.1f}%（>0 ≤20%）"
    if not ok:
        return False, reason
    return True, reason


# ================= 推送 =================

def push_wecom(items):
    """推送到企微 webhook（markdown）"""
    lines = ["## 🎯 观察池开仓信号", ""]
    for it in items:
        s = it["stock"]
        q = it["quote"]
        grade = s["grade"]
        pos = s["position_hint"] or "-"
        lines.append(f"**{s['stock_name']}**（{s['stock_code']}）· {s['theme_name']}")
        lines.append(f"> 级别：{'✅ A级' if grade == 'A级' else '⏳ B级触发'}｜现价 **{q['price']:.2f}**"
                     f"｜止损 {s['stop_px']:.2f}｜目标 {s['target_px']:.2f}｜R:R {s['rr']}"
                     f"｜建议仓位 {pos}")
        if grade == "A级" and s["entry_start"]:
            lines.append(f"> 入场区间：{s['entry_start']:.2f} ~ {s['entry_end']:.2f}")
        lines.append(f"> 判定：{it['reason']}")
        lines.append("")
    if DRY_RUN:
        print("[" + "\n".join(lines) + "]")
        return True
    payload = {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}
    req = urllib.request.Request(
        WEBHOOK_URL, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10).read().decode()
        print(f"[push] webhook resp: {resp}", file=sys.stderr)
        return json.loads(resp).get("errcode") == 0
    except Exception as e:
        print(f"[warn] 企微推送失败: {e}", file=sys.stderr)
        return False


# ================= 主流程 =================

def main():
    stocks = load_watch_pool()
    if not stocks:
        print("观察池无 watching 标的，静默退出")
        return 0
    codes = [s["stock_code"] for s in stocks]
    quotes = get_quote_batch(codes)

    hit = []
    for s in stocks:
        q = quotes.get(s["stock_code"])
        if not q or not q.get("price"):
            print(f"[skip] {s['stock_code']} 行情缺失", file=sys.stderr)
            continue
        kline = get_kline(s["stock_code"], limit=30)
        ok, reason = judge(s, q, kline)
        tag = "✅" if ok else "—"
        print(f"{tag} {s['pool_date']} {s['grade']} {s['stock_name']} {s['stock_code']}: {reason}")
        if ok:
            hit.append({"stock": s, "quote": q, "reason": reason, "kline": kline})

    if not hit:
        print("无符合开仓条件标的，静默退出")
        return 0

    push_wecom(hit)
    if not DRY_RUN:
        for it in hit:
            s = it["stock"]
            mark_triggered(s["stock_code"], s["pool_date"])
            print(f"[db] {s['stock_name']} status → triggered ({s['pool_date']})", file=sys.stderr)
    print(f"推送 {len(hit)} 只开仓信号")
    return 0


if __name__ == "__main__":
    sys.exit(main())