#!/usr/bin/env python3
"""
退潮主线监控（faded theme watch）— 每日17:30任务

设计原则：主线退潮 ≠ 永久结束。只要行业趋势还在（板块指数趋势未破坏），
退潮主线保持监控，等待重新启动信号 → 回到主线候选池。

数据流：
1. 读 faded_theme_watch 表 → watching/reviving 状态的主线
2. 解析 related_sectors.sectors 板块代码（pt0180xxx）
3. 腾讯 fqkline 直连拉板块日K线（60天）→ 自算 MA20/MA60/斜率/20日涨幅
4. 读当日 plate 文件表格2，查主线板块上榜数（重启信号）
5. 板块级判定 → 主线级信号 → 更新 faded_theme_watch 状态 + 写 checkins
6. 生成报告 md + 推送企微

用法：
  python3 scripts/faded_theme_monitor.py                 # 今日
  python3 scripts/faded_theme_monitor.py --date 2026-08-21   # 指定日期
  python3 scripts/faded_theme_monitor.py --dry-run       # 只算不写库
"""
import sys
import os
import re
import json
import argparse
import urllib.request
from bisect import bisect_right
from datetime import date, datetime, timedelta

# ── MySQL 配置（与 fetch_trend.py 一致）──
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "portfolio",
    "charset": "utf8mb4",
}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATE_DIR = os.path.join(BASE_DIR, "data", "plate")
REVIVE_LIST_MIN = 3  # 板块在表格2上榜 ≥3 只 = 重启候选信号（与三步启动确认一致）
Q_ZGB_START = 0.57
Q_ZGB_SPREAD = 0.82
Q_RANK_STRONG = 0.20
Q_LB_STRONG = 0.13

KLINE_URL = ("https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get"
             "?code={code}&ktype=day&fqtype=qfq&limit=240&fromDate={begin}&toDate={end}")

# 腾讯 hy2 板块排行（count=200 全量约124行业；zgb 字段 = "涨股数/总股数"，用于算涨股比）
RANK_URL = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?"
            "board_type=hy2&sort_type=PriceRatioD20&direct=down"
            "&selector=not_in_market%3Dgn_fg&count=200")

RATIO_HISTORY_DAYS = 3       # 涨股比取近3个交易日（含当日）均值，替代单日快照

SIGNAL_STRONG = {"trend_ok", "revive_candidate"}
SIGNAL_WEAK = {"trend_weak", "trend_broken"}

# 企微推送（个股分析机器人）
WECOM_URL = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?"
             "key=<WECOM_WEBHOOK_KEY>")

STATUS_ICON = {"watching": "👀", "reviving": "🚀", "revived": "✅", "dropped": "❌"}


try:
    import pymysql
except ImportError:
    print("需要安装 pymysql"); sys.exit(1)


def get_db():
    conn = pymysql.connect(**DB_CONFIG)
    conn.autocommit(True)
    return conn


def load_watch_list():
    """读监控池 watching/reviving 状态的主线"""
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id, theme_name, related_sectors, peak_count, fade_date, "
                "fade_reason, watch_status, watch_days, checkin_count, reconcile "
                "FROM faded_theme_watch WHERE watch_status IN ('watching','reviving') "
                "ORDER BY fade_date, id")
            return cur.fetchall()
    finally:
        conn.close()


def extract_sectors(related_sectors_json):
    try:
        sectors = json.loads(related_sectors_json)
        if isinstance(sectors, dict):
            return sectors.get("sectors", [])
    except Exception:
        pass
    return []


def get_sector_kline(sector_code, end_date):
    """proxy.finance.qq.com stockinfoquery 拉板块日K线（finance_plugin 验证过的接口）
    返回 [(date, open, close, high, low), ...] 升序
    """
    begin = (end_date - timedelta(days=180)).strftime("%Y-%m-%d")
    url = KLINE_URL.format(code=sector_code, begin=begin, end=end_date.strftime("%Y-%m-%d"))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        data = json.loads(text)
        nodes = data.get("data", {}).get("nodes", [])
        rows = []
        for n in nodes:
            try:
                rows.append((n["date"], float(n["open"]), float(n["last"]), float(n["high"]), float(n["low"])))
            except (KeyError, ValueError, TypeError):
                continue
        return rows
    except Exception as e:
        print(f"  ⚠️ 拉取 {sector_code} K线失败: {e}")
        return []


def _f(v):
    """安全转 float，失败返回 None"""
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def load_cross_section_quantiles(conn, today):
    """读取当日行业横截面，返回总行数与字段升序分位函数。"""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT zgb_ratio, lb, rank_daily FROM sector_rank_daily WHERE rank_date=%s",
            (today,))
        rows = cur.fetchall()

    quantiles = {}
    for field in ("zgb_ratio", "lb"):
        values = sorted(float(row[field]) for row in rows if row[field] is not None)
        quantiles[field] = (
            lambda value, values=values: bisect_right(values, value) / len(values)
            if values and value is not None else None
        )
    return len(rows), quantiles


def is_low_candidate(sector, quantiles):
    zgb_quantile = quantiles["zgb_ratio"]
    ratio_quantile = zgb_quantile(sector.get("ratio"))
    today_quantile = zgb_quantile(sector.get("ratio_today"))
    return (ratio_quantile is not None and ratio_quantile <= Q_ZGB_START
            and today_quantile is not None and today_quantile <= Q_ZGB_SPREAD)


def is_strong_sector(sector, cross_section_count, quantiles):
    lb_quantile = quantiles["lb"](sector.get("lb"))
    rank_daily = sector.get("rank_daily")
    return (sector["signal"] == "trend_ok"
            and rank_daily is not None and cross_section_count > 0
            and rank_daily / cross_section_count <= Q_RANK_STRONG
            and lb_quantile is not None and lb_quantile >= Q_LB_STRONG)


def fetch_all_rank_meta():
    """拉取 hy2 全A行业排行（count=200 全量约124行业）。

    返回 {code: {name, zdf, zdf_d5, zdf_d20, zdf_d60, zgb_ratio, zgb_up, zgb_total,
                 lb(量比), hsl, rank_daily(当日涨幅位次), rank_d20(20日涨幅位次)}}
    rank_d20 = 接口 list 顺序位次（sort_type=PriceRatioD20 desc，首位=20日最强）
    rank_daily = 按当日涨幅 zdf 降序重排位次
    """
    try:
        req = urllib.request.Request(RANK_URL, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        data = json.loads(text)
        rank_list = data.get("data", {}).get("rank_list", [])
        meta = {}
        for idx, item in enumerate(rank_list, 1):
            code = item.get("code", "")
            zgb = str(item.get("zgb", ""))
            ratio = zgb_up = zgb_total = None
            if "/" in zgb:
                try:
                    up_s, tot_s = zgb.split("/")
                    zgb_up, zgb_total = int(up_s), int(tot_s)
                    if zgb_total > 0:
                        ratio = round(zgb_up / zgb_total * 100, 1)
                except (ValueError, ZeroDivisionError):
                    pass
            meta[code] = {
                "name": item.get("name", ""),
                "zdf": _f(item.get("zdf")), "zdf_d5": _f(item.get("zdf_d5")),
                "zdf_d20": _f(item.get("zdf_d20")), "zdf_d60": _f(item.get("zdf_d60")),
                "zgb_ratio": ratio, "zgb_up": zgb_up, "zgb_total": zgb_total,
                "lb": _f(item.get("lb")), "hsl": _f(item.get("hsl")),
                "rank_d20": idx, "rank_daily": None,
            }
        # 当日涨幅位次（降序，None 排末尾）
        daily_sorted = sorted(meta.items(), key=lambda kv: (kv[1]["zdf"] is None, -(kv[1]["zdf"] or 0)))
        for rk, (code, _) in enumerate(daily_sorted, 1):
            meta[code]["rank_daily"] = rk
        return meta
    except Exception as e:
        print(f"  ⚠️ 拉取 hy2 全A排行失败: {e}")
        return {}


def save_rank_snapshot(conn, rank_meta, rank_date):
    """UPSERT 当日全A排行快照到 sector_rank_daily（3日涨股比趋势/全A位次/池外提示数据源）。"""
    if not rank_meta:
        return 0
    n = 0
    with conn.cursor() as cur:
        for code, m in rank_meta.items():
            cur.execute(
                "INSERT INTO sector_rank_daily (rank_date, code, name, zdf, zdf_d5, zdf_d20, zdf_d60, "
                "zgb_ratio, zgb_up, zgb_total, lb, hsl, rank_daily, rank_d20) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), zdf=VALUES(zdf), zdf_d5=VALUES(zdf_d5), "
                "zdf_d20=VALUES(zdf_d20), zdf_d60=VALUES(zdf_d60), zgb_ratio=VALUES(zgb_ratio), "
                "zgb_up=VALUES(zgb_up), zgb_total=VALUES(zgb_total), lb=VALUES(lb), hsl=VALUES(hsl), "
                "rank_daily=VALUES(rank_daily), rank_d20=VALUES(rank_d20)",
                (rank_date, code, m["name"], m["zdf"], m["zdf_d5"], m["zdf_d20"], m["zdf_d60"],
                 m["zgb_ratio"], m["zgb_up"], m["zgb_total"], m["lb"], m["hsl"],
                 m["rank_daily"], m["rank_d20"]))
            n += 1
    return n


def sector_info_from_rank(conn, sector_codes, rank_meta, rank_date):
    """从全A排行 meta + sector_rank_daily 历史构建板块信息（涨股比3日趋势）。

    返回 {code: {ratio(近3交易日均值), ratio_today(今日), ratio_hist(升序列表),
                 rank_daily, rank_d20, lb(量比), name}}
    - 3日均值数据不足3天时用可用天数均值（仅1天=单日降级），报告标注积累中
    """
    hist_map = {}
    if conn and sector_codes:
        try:
            ph = ",".join(["%s"] * len(sector_codes))
            sql = ("SELECT code, zgb_ratio FROM sector_rank_daily "
                   "WHERE code IN (" + ph + ") AND rank_date<=%s AND zgb_ratio IS NOT NULL "
                   "ORDER BY rank_date DESC, id DESC")
            with conn.cursor() as cur:
                cur.execute(sql, list(sector_codes) + [rank_date])
                for code, ratio in cur.fetchall():
                    hist_map.setdefault(code, []).append(ratio)
        except Exception as e:
            print(f"  ⚠️ 读取涨股比历史失败: {e}")
    info = {}
    for code in sector_codes:
        m = rank_meta.get(code) or {}
        hist = [r for r in hist_map.get(code, []) if r is not None][:RATIO_HISTORY_DAYS]
        hist.reverse()  # 升序（旧→新）
        ratio_today = m.get("zgb_ratio") if m.get("zgb_ratio") is not None else (hist[-1] if hist else None)
        if ratio_today is not None:
            if hist and abs(hist[-1] - ratio_today) > 0.01:
                hist.append(ratio_today)
            elif not hist:
                hist.append(ratio_today)
        hist = hist[-RATIO_HISTORY_DAYS:]
        ratio = round(sum(hist) / len(hist), 1) if hist else None
        info[code] = {
            "ratio": ratio, "ratio_today": ratio_today, "ratio_hist": hist,
            "rank_daily": m.get("rank_daily"), "rank_d20": m.get("rank_d20"),
            "lb": m.get("lb"), "name": m.get("name", ""),
        }
    return info


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def analyze_sector(rows):
    """板块级趋势判定：
    close vs MA20/MA60 + MA20斜率(5日) + 20日涨幅
    返回 dict 或 None（数据不足）
    """
    if len(rows) < 25:
        return None
    closes = [r[2] for r in rows]
    last = rows[-1]
    close = last[2]
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    # MA20 斜率：今日 MA20 vs 5日前 MA20
    ma20_now = sma(closes, 20)
    ma20_5ago = sma(closes[:-5], 20) if len(closes) > 25 else None
    ma20_slope = "flat"
    if ma20_now and ma20_5ago:
        ma20_slope = "up" if ma20_now > ma20_5ago * 1.001 else ("down" if ma20_now < ma20_5ago * 0.999 else "flat")
    # 20日涨幅：今收盘 vs 20交易日前收盘
    d20 = None
    if len(closes) > 20:
        d20 = (close / closes[-21] - 1) * 100 if closes[-21] else None
    # 板块级信号（判据表：trend_broken = 收盘 < MA60 且 MA20 斜向下）
    # 收盘 < MA60 但 MA20 未向下 → 趋势走弱未破坏（斜率向上=可能修复中，不判死）
    if close >= ma20:
        signal = "trend_ok"
    elif ma60 and close >= ma60:
        signal = "trend_weak"
    elif ma60 and ma20_slope == "down":
        signal = "trend_broken"
    else:
        signal = "trend_weak"
    return {
        "last_date": last[0], "close": close, "ma20": ma20_now, "ma60": ma60,
        "ma20_slope": ma20_slope, "d20": d20, "signal": signal,
        "above_ma20": close >= ma20_now if ma20_now else None,
        "above_ma60": close >= ma60 if ma60 else None,
    }


def parse_plate_table2(filepath):
    """解析表格2：板块代码 → count（上榜数）"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"## 表格2：二级行业板块统计.*?\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
    if not m:
        return {}
    result = {}
    pattern = r"\|\s*\d+\s*\|\s*([^|]+?)\s*\|\s*(pt\d+)\s*\|\s*(\d+)\s*\|"
    for mm in re.finditer(pattern, m.group(1)):
        result[mm.group(2).strip()] = int(mm.group(3))
    return result


def theme_signal(sector_results, list_count, quantiles):
    """主线级信号聚合（行业级强信号不下沉）：
    - revive_candidate：上榜数≥3 且任一 trend_ok 板块满足涨股比当日分位阈值
    - 行业级强信号（⭐）：任一板块当日位次分位与量比分位满足强信号阈值 + trend_ok
      （局部启动不被聚合信号一刀切掩盖）
    - trend_ok：≥50%板块 trend_ok
    - trend_broken：半数以上板块 trend_broken（n=2 需全部破位；n=3 需≥2）且无板块 trend_ok。
      与 trend_ok 的"≥半数"刻意不对称——破位是更严重的尾部风险，需更高证据标准，单板块破位不放大为主线破位
    - 其余 trend_weak
    """
    if not sector_results:
        return "trend_broken"
    ok = sum(1 for s in sector_results if s["signal"] == "trend_ok")
    broken = sum(1 for s in sector_results if s["signal"] == "trend_broken")
    n = len(sector_results)
    if list_count >= REVIVE_LIST_MIN and ok >= 1:
        # 低位启动特征 = 涨股比3日均值与当日值均不高于当日横截面扩散分位
        low_ratio_ok = [s for s in sector_results
                        if s["signal"] == "trend_ok" and is_low_candidate(s, quantiles)]
        if low_ratio_ok:
            return "revive_candidate"
    # 行业级强信号（⭐）：局部启动即使主线程聚合不足也不压为 weak
    if any(s.get("strong") for s in sector_results):
        return "trend_ok"
    if ok >= n / 2:
        return "trend_ok"
    if broken > n / 2 and ok == 0:
        return "trend_broken"
    return "trend_weak"


def update_status(conn, theme_name, new_signal, list_count, broken_streak, watch_days, prev_status, check_date):
    """更新主表状态。
    - revive_candidate 首次 → reviving（待次日确认）
    - reviving + revive_candidate 连续 → revived（转正，重回主线候选）
    - reviving + 非candidate → 回 watching（候选失败）
    - trend_broken 连续3天 → dropped（出池）
    幂等：当日已检查过（重复运行）→ watch_days/checkin_count 不重复累加。
    """
    # 幂等判断：同日重跑不累加 watch_days/checkin_count
    with conn.cursor() as cur:
        cur.execute("SELECT last_check_date FROM faded_theme_watch WHERE theme_name=%s", (theme_name,))
        row = cur.fetchone()
    is_repeat = bool(row and row[0] and row[0] == check_date)
    if is_repeat:
        if new_signal == "trend_broken":
            note = f"行业趋势破坏第{broken_streak+1}天"
        elif new_signal == "revive_candidate" and prev_status == "reviving":
            note = "出现重启信号（板块上榜+趋势转好），待次日确认"
        else:
            note = ""
        return prev_status, note + "（同日重跑，状态保持）"
    if not is_repeat:
        watch_days += 1
    if new_signal == "revive_candidate":
        if prev_status == "reviving":
            status = "revived"
            note_extra = "重启信号连续2天确认，主线转正，建议明日16:00推理重走三层过滤纳入候选"
        else:
            status = "reviving"
            note_extra = "出现重启信号（板块上榜+趋势转好），待次日确认"
    elif new_signal == "trend_broken":
        if broken_streak >= 2:  # 破位连续3天（含今日）→ 出池
            status = "dropped"
            note_extra = "行业趋势破坏连续3天，移出监控池"
        else:
            status = "watching"
            note_extra = f"行业趋势破坏第{broken_streak+1}天"
    else:
        status = "watching"
        note_extra = ""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE faded_theme_watch SET watch_status=%s, watch_days=%s, "
            "last_check_date=%s, last_signal_type=%s, checkin_count=checkin_count+%s "
            "WHERE theme_name=%s",
            (status, watch_days, check_date, new_signal, 1 if not is_repeat else 0, theme_name))
    return status, note_extra


def save_checkin(conn, theme_name, sector_codes, sector_results, list_count, signal, note, check_date):
    """写每日检查快照（幂等：UNIQUE(theme_name, check_date) 冲突时覆盖当日快照）"""
    first = sector_results[0] if sector_results else {}
    close_vs_ma20 = "above" if first.get("above_ma20") else ("below" if first.get("above_ma20") is False else None)
    close_vs_ma60 = "above" if first.get("above_ma60") else ("below" if first.get("above_ma60") is False else None)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO faded_theme_watch_checkins "
            "(theme_name, check_date, sector_codes, close_vs_ma20, close_vs_ma60, "
            "ma20_slope, d20_pct, list_count, signal_type, note) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE "
            "sector_codes=VALUES(sector_codes), close_vs_ma20=VALUES(close_vs_ma20), "
            "close_vs_ma60=VALUES(close_vs_ma60), ma20_slope=VALUES(ma20_slope), "
            "d20_pct=VALUES(d20_pct), list_count=VALUES(list_count), "
            "signal_type=VALUES(signal_type), note=VALUES(note)",
            (theme_name, check_date, ",".join(sector_codes), close_vs_ma20, close_vs_ma60,
             first.get("ma20_slope"), first.get("d20"), list_count, signal, note))


def _trading_days_between(d1, d2):
    """d1→d2 之间工作日数（含 d2 不含 d1，周一~周五为交易日）；>1 = 中间存在缺检交易日"""
    days = 0
    cur = d1 + timedelta(days=1)
    while cur <= d2:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def get_broken_streak(conn, theme_name, today=None):
    """查最近连续 trend_broken 天数（不含今日）。
    缺检日打断连续：相邻 checkin 间隔 >1 个交易日 → 计数重置（防断档被误计为连续破位）。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT check_date, signal_type FROM faded_theme_watch_checkins "
                "WHERE theme_name=%s ORDER BY check_date DESC LIMIT 10", (theme_name,))
            rows = cur.fetchall()
        if today is not None:
            rows = [row for row in rows if row[0] != today]
        streak = 0
        for i, (cd, sig) in enumerate(rows):
            if sig != "trend_broken":
                break
            if i > 0:
                prev_date = rows[i - 1][0]
                if not isinstance(cd, date):
                    cd = datetime.strptime(str(cd), "%Y-%m-%d").date()
                if not isinstance(prev_date, date):
                    prev_date = datetime.strptime(str(prev_date), "%Y-%m-%d").date()
                if _trading_days_between(cd, prev_date) > 1:
                    break
            streak += 1
        return streak
    except Exception:
        return 0


def push_wecom(text):
    """推送企微（仅重启/出池等关键信号）"""
    try:
        payload = json.dumps({"msgtype": "markdown", "markdown": {"content": text}}).encode("utf-8")
        req = urllib.request.Request(WECOM_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        return resp.get("errcode") == 0
    except Exception as e:
        print(f"  ⚠️ 企微推送失败: {e}")
        return False


def auto_register_faded(conn, today):
    """自动登记退潮主线入池（自治，不依赖16:00推理主动登记）：
    扫描 industry_themes 当日 + 近3日 fading/ended 状态的主线，
    以及当日被数量上限挤出（观察线降级）的主线，若池内不存在则 INSERT。
    结构性淘汰的伪主线（related_sectors 无 sectors 板块）不入池。

    P2 §2.4：fade_reason 使用扩展枚举（向后兼容，旧值不动）：
      - 状态机 ended/fading → tech_fade（技术退潮）
      - 链身份漂移        → identity_drift（核心 sectors 漂移）
      - 数量上限挤出      → cap_exceed（子链上限挤出观察线降级）
    新登记才用新枚举，既有行 fade_reason 不修改。
    返回新登记清单。
    """
    newbies = []
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT theme_name, status, consecutive_days, peak_count, peak_date, related_sectors "
            "FROM industry_themes WHERE theme_date BETWEEN %s AND %s "
            "AND status IN ('fading','ended') AND is_legacy = 0 ORDER BY theme_date DESC LIMIT 20",
            ((today - timedelta(days=3)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")))
        rows = cur.fetchall()
        # 已入池名单
        cur.execute("SELECT theme_name FROM faded_theme_watch")
        existing = {r["theme_name"] for r in cur.fetchall()}

    for r in rows:
        name = r["theme_name"]
        if name in existing:
            continue
        sectors = extract_sectors(r.get("related_sectors", "{}"))
        sec_codes = [s.get("code", "") for s in sectors if isinstance(s, dict) and s.get("code")]
        if not sec_codes:
            print(f"  ⏭️ {name}（{r['status']}）无 sectors 板块 → 结构性伪主线不入池")
            continue
        # P2 §2.4：新登记使用扩展枚举 fade_reason
        # ended/fading = 技术退潮（tech_fade），数量上限 = cap_exceed，身份漂移 = identity_drift
        fade_reason = f"tech_fade（{r['status']}，峰值{r.get('peak_count') or 0}只）"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO faded_theme_watch (theme_name, related_sectors, peak_count, peak_date, fade_date, fade_reason) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (name, r["related_sectors"], r.get("peak_count") or 0, r.get("peak_date"), today, fade_reason))
        newbies.append(name)
        print(f"  🆕 {name} 自动登记入池（{r['status']}，fade_reason={fade_reason}）")

    # 近3日内最近一条记录仍 active/peaking 但当日缺席的链，才视为族内挤出兜底对象。
    # ended 链按规则不再重复记录，不进入差集；当日表为空则跳过全量兜底。
    # 当日主线表为空说明推理未跑或全失败，必须跳过，避免把全量旧链误登记入池。
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT DISTINCT theme_name FROM industry_themes WHERE theme_date = %s AND is_legacy = 0",
            (today.strftime("%Y-%m-%d"),))
        today_names = {r["theme_name"] for r in cur.fetchall()}
        if not today_names:
            print("⏭️ 族内挤出差集检测跳过（当日 industry_themes 无记录，16:00 推理未跑/全失败）")
            return newbies

        cur.execute(
            "SELECT t.theme_name "
            "FROM ("
            "  SELECT theme_name, MAX(theme_date) AS d "
            "  FROM industry_themes "
            "  WHERE theme_date BETWEEN %s AND %s AND is_legacy = 0 "
            "  GROUP BY theme_name"
            ") x "
            "JOIN industry_themes t ON t.theme_name = x.theme_name AND t.theme_date = x.d "
            "WHERE t.status IN ('active','peaking')",
            ((today - timedelta(days=3)).strftime("%Y-%m-%d"),
             (today - timedelta(days=1)).strftime("%Y-%m-%d")))
        active_recent_names = {r["theme_name"] for r in cur.fetchall()}

    missing = sorted(active_recent_names - today_names)
    if len(missing) > 10:
        print(f"⚠️ 族内挤出链 {len(missing)} 条 > 10，疑似异常（16:00 部分失败？），仅登记前 10 条")
        missing = missing[:10]

    for name in missing:
        if name in existing:
            continue
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT related_sectors, peak_count, peak_date FROM industry_themes "
                "WHERE theme_name = %s AND is_legacy = 0 AND theme_date <= %s "
                "ORDER BY theme_date DESC LIMIT 1",
                (name, (today - timedelta(days=1)).strftime("%Y-%m-%d")))
            r = cur.fetchone()
        if not r:
            continue
        sectors = extract_sectors(r.get("related_sectors", "{}"))
        sec_codes = [s.get("code", "") for s in sectors if isinstance(s, dict) and s.get("code")]
        if not sec_codes:
            print(f"  ⏭️ {name}（族内挤出）无 sectors 板块 → 不入池")
            continue
        peak_count = r.get("peak_count") or 0
        fade_reason = f"cap_exceed（族内挤出，峰值{peak_count}只）"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO faded_theme_watch (theme_name, related_sectors, peak_count, peak_date, fade_date, fade_reason) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (name, r["related_sectors"], peak_count, r.get("peak_date"), today, fade_reason))
        newbies.append(name)
        print(f"  🆕 {name} 族内挤出登记入池（cap_exceed，峰值{peak_count}只）")
    return newbies


def sync_sectors_from_themes(conn, today, dry_run=False):
    """监控池口径与主线表对齐（每日在 auto_register 之后、load_watch_list 之前执行）。
    登记时 related_sectors 只是主线表历史某天的快照，主线表换血后板块集合漂移，
    会导致下游（17:30判据 / 18:00候选取成分股）混入已不属于该主线的板块。
    规则：
    - 主线表最近同名记录 ≤3 日 → 用最新 related_sectors 覆盖监控池（换血/重启同步口径）
    - 3 < 间隔 ≤ 5 日          → 保旧（主线表暂未跟踪，保留登记快照）并在报告中标注口径陈旧
    - 间隔 > 5 日或无记录       → 丢弃（置 dropped，退出监控；避免旧快照无限期过时）
    只处理 watching/reviving/revived（与 18:00 fetch_faded_watch_leaders.py 读表范围一致，
    revived 主线的 sectors 会被 18:00 用于 A级建池取成分股，同样必须口径同步）；
    已 dropped 的行不再触碰。dry_run 只打印决策不写库。
    返回 (报告文案列表, 丢弃清单)。
    """
    notes, dropped = [], []
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT id, theme_name, related_sectors FROM faded_theme_watch "
                    "WHERE watch_status IN ('watching','reviving','revived')")
        pool = cur.fetchall()
        for t in pool:
            name = t["theme_name"]
            cur.execute("SELECT MAX(theme_date) AS last_seen FROM industry_themes WHERE theme_name=%s", (name,))
            row = cur.fetchone()
            last_seen = row["last_seen"] if row else None
            gap = None if not last_seen else (today - last_seen).days
            if gap is None or gap > 5:
                if not dry_run:
                    cur.execute("UPDATE faded_theme_watch SET watch_status='dropped' WHERE id=%s", (t["id"],))
                dropped.append(name)
                notes.append(f"❌ {name} 丢弃（主线表最近记录 {last_seen or '无'}，失联 {gap if gap is not None else 'N/A'} 日 > 5 日，停止监控）")
            elif gap <= 3:
                cur.execute("SELECT related_sectors FROM industry_themes WHERE theme_name=%s AND theme_date=%s",
                            (name, last_seen))
                new_row = cur.fetchone()
                if new_row and new_row["related_sectors"] and new_row["related_sectors"] != t["related_sectors"]:
                    if not dry_run:
                        cur.execute("UPDATE faded_theme_watch SET related_sectors=%s WHERE id=%s",
                                    (new_row["related_sectors"], t["id"]))
                    old = [s.get("name", "") for s in extract_sectors(t["related_sectors"])]
                    new = [s.get("name", "") for s in extract_sectors(new_row["related_sectors"])]
                    notes.append(f"↻ {name} 口径同步 [{','.join(old)}] → [{','.join(new)}]（主线表 {last_seen} 最新口径）")
                else:
                    print(f"  ✓ {name} 口径一致（主线表 {last_seen}）")
            else:
                notes.append(f"⏸ {name} 保旧（主线表最近记录 {last_seen}，间隔 {gap} 日，3<gap≤5 暂不覆盖）")
    return notes, dropped


def prev_trading_day(conn, today):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(rank_date) FROM sector_rank_daily WHERE rank_date < %s", (today,))
        row = cur.fetchone()
    return row[0] if row else None


def reconcile_mainline(conn, today, dry_run=False):
    """按当日主线池方向敏感标记同名链；dry_run 只打印决策，不写库。"""
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT DISTINCT theme_name FROM industry_themes "
            "WHERE theme_date=%s AND is_legacy=0 AND status IN ('active','peaking')",
            (today,))
        active_today = {row["theme_name"] for row in cur.fetchall()}
        cur.execute(
            "SELECT id, theme_name, watch_status, reconcile, reconcile_days FROM faded_theme_watch "
            "WHERE watch_status IN ('watching','reviving','revived') ORDER BY id")
        rows = cur.fetchall()

    decisions = []
    for row in rows:
        name = row["theme_name"]
        if name not in active_today:
            if row["reconcile"] is not None or row["reconcile_days"]:
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE faded_theme_watch SET reconcile=NULL, reconcile_days=0, updated_at=updated_at WHERE id=%s",
                            (row["id"],))
                print(f"  ↩️ {name} 解除主线调和（当日不在 active/peaking 主线池）")
            continue

        with conn.cursor() as cur:
            cur.execute(
                "SELECT signal_type FROM faded_theme_watch_checkins "
                "WHERE theme_name=%s AND check_date=%s",
                (name, today))
            checkin = cur.fetchone()
        if not checkin:
            continue

        signal_type = checkin[0]
        if signal_type in SIGNAL_WEAK:
            reconcile = "mainline_conflict"
        elif signal_type in SIGNAL_STRONG:
            reconcile = "mainline_aligned"
        else:
            if row["reconcile"] is not None or row["reconcile_days"]:
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE faded_theme_watch SET reconcile=NULL, reconcile_days=0, updated_at=updated_at WHERE id=%s",
                            (row["id"],))
                print(f"  ↩️ {name} 解除主线调和（非冲突/一致方向交集）")
            continue

        current_days = row["reconcile_days"] or 0
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(check_date) FROM faded_theme_watch_checkins "
                "WHERE theme_name=%s AND check_date < %s",
                (name, today))
            checkin_row = cur.fetchone()
        prev_check_date = checkin_row[0] if checkin_row else None
        new_days = (
            current_days + 1
            if current_days > 0 and row["reconcile"] == reconcile
            and prev_check_date == prev_trading_day(conn, today)
            else 1
        )
        decisions.append({
            "theme_name": name,
            "reconcile": reconcile,
            "reconcile_days": new_days,
            "signal_type": signal_type,
        })

        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE faded_theme_watch SET reconcile=%s, reconcile_days=%s, updated_at=updated_at WHERE id=%s",
                    (reconcile, new_days, row["id"]))
        print(f"  ✅ {name} {reconcile}（{new_days}日）")

    if dry_run:
        conflicts = [decision for decision in decisions if decision["reconcile"] == "mainline_conflict"]
        aligned = [decision for decision in decisions if decision["reconcile"] == "mainline_aligned"]
        print(f"⚠️ 同名冲突（{len(conflicts)} 条）：{' / '.join(d['theme_name'] for d in conflicts) or '无'}")
        for decision in conflicts:
            print(f"- {decision['theme_name']}：16:00 主线池 active/peaking（信号层）vs 17:30 {decision['signal_type']}（指数层）；取主线口径为准")
        print(f"✅ 同步主线（{len(aligned)} 条）：{' / '.join(d['theme_name'] for d in aligned) or '无'}")
        for decision in aligned:
            print(f"- {decision['theme_name']}：本池 {decision['signal_type']} 与主线池 active/peaking 方向一致，候选照常产出（见 18:00）")
    return decisions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="只算不写库")
    parser.add_argument("--snapshot-only", action="store_true",
                        help="只拉全A排行落库快照后退出（补历史数据点用，不碰监控池状态/不写报告）")
    args = parser.parse_args()

    today = date.fromisoformat(args.date) if args.date else date.today()
    label = today.strftime("%Y%m%d")

    # 全A排行快照数据源：3日涨股比趋势/全A位次/池外提示
    rank_meta = fetch_all_rank_meta()

    if args.snapshot_only:
        if not rank_meta:
            print("⚠️ 全A排行拉取失败，快照未落库")
            sys.exit(1)
        conn = get_db()
        try:
            n_saved = save_rank_snapshot(conn, rank_meta, today)
            print(f"✅ 全A排行快照已落库: {n_saved} 行业（{today}）")
        finally:
            conn.close()
        return

    if not rank_meta:
        print("  ⚠️ 全A排行拉取失败，本次无全A位次/涨股比数据（降级运行）")

    plate_file = os.path.join(PLATE_DIR, f"plate_{label}.md")
    if not os.path.exists(plate_file):
        print(f"⚠️ 板块文件不存在: {plate_file}，监控中止（无当日上榜数据）")
        sys.exit(1)

    plate_t2 = parse_plate_table2(plate_file)
    conn = get_db()
    cross_section_count, quantiles = load_cross_section_quantiles(conn, today)
    # 全A排行快照落库（3日涨股比趋势积累）；dry-run 不写库
    if not args.dry_run and rank_meta:
        n_saved = save_rank_snapshot(conn, rank_meta, today)
        print(f"📊 全A排行快照已落库: {n_saved} 行业（{today}）")
    # 自动登记新退潮主线（幂等：已入池的跳过）；dry-run 不写库
    if not args.dry_run:
        newbies = auto_register_faded(conn, today)
        if newbies:
            print(f"🆕 本次新登记 {len(newbies)} 条退潮主线: {', '.join(newbies)}")

    lines = []
    pushes = []
    lines.append(f"## 退潮主线监控报告 {today}")
    lines.append("")
    lines.append("> 原则：主线退潮 ≠ 永久结束，行业趋势还在就保持关注。")
    lines.append("> 判据：板块指数收盘 vs MA20/MA60、MA20斜率、20日涨幅、表格2上榜数、全A位次(RankD/RankD20)、板块3日涨股比当日横截面分位、⭐=当日位次前20%分位且量比强分位。")
    lines.append("")

    # 口径同步：主线表换血后板块集合漂移修复（见 sync_sectors_from_themes 规则）
    sync_notes, dropped_names = sync_sectors_from_themes(conn, today, dry_run=args.dry_run)
    for sn in sync_notes:
        print(f"↻ {sn}")
    if sync_notes:
        lines.append("### 口径同步")
        lines += [f"- {sn}" for sn in sync_notes]
        lines.append("")
    for dn in dropped_names:
        pushes.append(f"❌ **{dn} 移出监控池**\n> 主线表超过5日无该主线记录（行业逻辑已不再跟踪），监控池丢弃，停止监控。")

    watch_list = load_watch_list()
    if not watch_list:
        print("ℹ️ 监控池为空，无退潮主线需要检查")
        return

    # 报告格式辅助（3日涨股比趋势/全A位次/量比/⭐强信号；顶层定义供 watching 循环与 revived 渲染共用）
    def _fmt_ratio_hist(r):
        hist = r.get("ratio_hist") or []
        if not hist:
            return "N/A"
        s = "→".join(f"{v:.0f}%" for v in hist)
        if r.get("ratio") is not None:
            s += f"均{r['ratio']:.0f}%"
        return s

    def _sector_line(r):
        p = [r["signal"], f"涨股比{_fmt_ratio_hist(r)}"]
        if r.get("rank_daily") is not None:
            p.append(f"RankD{r['rank_daily']}/{cross_section_count}")
        if r.get("rank_d20") is not None:
            p.append(f"RankD20 {r['rank_d20']}/{cross_section_count}")
        if r.get("lb") is not None:
            p.append(f"量比{r['lb']:.1f}")
        if r.get("strong"):
            p.append("⭐")
        # 属性分号分隔、不含嵌套括号（保证 18:00 解析正则稳定）
        return f"{r.get('name', '')}({'; '.join(p)})"

    def _is_low_candidate(r):
        return is_low_candidate(r, quantiles)

    def _low_txt(r):
        hist = r.get("ratio_hist") or []
        hist_txt = "→".join(f"{v:.0f}%" for v in hist) if hist else "N/A"
        return f"{r.get('name', '')}({hist_txt},3日均{r['ratio']:.0f}%,当日{r['ratio_today']:.0f}%)"

    for t in watch_list:
        name = t["theme_name"]
        sectors = extract_sectors(t["related_sectors"])
        sec_codes = [s["code"] for s in sectors if isinstance(s, dict) and s.get("code")]
        if not sec_codes:
            print(f"  ⚠️ {name} 无 sectors 板块代码，跳过")
            continue

        print(f"🔍 {name}（sectors: {','.join(sec_codes)}）")
        pinfo = sector_info_from_rank(conn, sec_codes, rank_meta, today) if rank_meta else {}
        results = []
        for code in sec_codes:
            rows = get_sector_kline(code, today)
            r = analyze_sector(rows)
            if r:
                r["code"], r["name"] = code, next((s.get("name", "") for s in sectors if isinstance(s, dict) and s.get("code") == code), "")
                pi = pinfo.get(code, {})
                r["ratio"] = pi.get("ratio")          # 3日涨股比均值（判据用）
                r["ratio_today"] = pi.get("ratio_today")
                r["ratio_hist"] = pi.get("ratio_hist") or []
                r["rank_daily"] = pi.get("rank_daily")  # 当日全A位次
                r["rank_d20"] = pi.get("rank_d20")      # 20日全A位次
                r["lb"] = pi.get("lb")                  # 量比
                r["strong"] = is_strong_sector(r, cross_section_count, quantiles)
                results.append(r)
                hist_txt = "→".join(f"{v:.0f}" for v in (r["ratio_hist"] or []))
                print(f"  {code}: signal={r['signal']} 涨股比3日[{hist_txt or 'N/A'}] 均值={r['ratio'] if r['ratio'] is not None else 'N/A'}% "
                      f"RankD={r['rank_daily']}/{cross_section_count} RankD20={r['rank_d20']}/{cross_section_count} lb={r['lb'] if r['lb'] is not None else 'N/A'} "
                      f"{'⭐' if r['strong'] else ''} close={r['close']:.1f} MA20={r['ma20']:.1f} "
                      f"slope={r['ma20_slope']} d20={r['d20'] if r['d20'] is not None else 0:.1f}%")

        list_count = sum(plate_t2.get(c, 0) for c in sec_codes)
        signal = theme_signal(results, list_count, quantiles)
        broken_streak = get_broken_streak(conn, name, today)
        watch_days = t["watch_days"]  # 原始值，累加由 update_status 幂等控制

        # 报告格式辅助（3日涨股比趋势/全A位次/量比/⭐强信号）
        def _fmt_ratio_hist(r):
            hist = r.get("ratio_hist") or []
            if not hist:
                return "N/A"
            s = "→".join(f"{v:.0f}%" for v in hist)
            if r.get("ratio") is not None:
                s += f"均{r['ratio']:.0f}%"
            return s

        def _sector_line(r):
            p = [r["signal"], f"涨股比{_fmt_ratio_hist(r)}"]
            if r.get("rank_daily") is not None:
                p.append(f"RankD{r['rank_daily']}/{cross_section_count}")
            if r.get("rank_d20") is not None:
                p.append(f"RankD20 {r['rank_d20']}/{cross_section_count}")
            if r.get("lb") is not None:
                p.append(f"量比{r['lb']:.1f}")
            if r.get("strong"):
                p.append("⭐")
            # 属性分号分隔、不含嵌套括号（保证 18:00 解析正则稳定）
            return f"{r.get('name', '')}({'; '.join(p)})"

        def _is_low_candidate(r):
            return is_low_candidate(r, quantiles)

        def _low_txt(r):
            hist = r.get("ratio_hist") or []
            hist_txt = "→".join(f"{v:.0f}%" for v in hist) if hist else "N/A"
            return f"{r.get('name', '')}({hist_txt},3日均{r['ratio']:.0f}%,当日{r['ratio_today']:.0f}%)"

        if args.dry_run:
            low_ratio_txt = "、".join(_low_txt(r) for r in results if _is_low_candidate(r))
            print(f"  → 主线信号={signal} 上榜数={list_count} 低位板块={low_ratio_txt or '无'}")
            continue

        status, note = update_status(conn, name, signal, list_count, broken_streak, watch_days, t["watch_status"], today)
        save_checkin(conn, name, sec_codes, results, list_count, signal, note or "", today)

        icon = STATUS_ICON.get(status, "👀")
        low_ratio_txt = "、".join(_low_txt(r) for r in results if _is_low_candidate(r))
        sector_txt = "; ".join(_sector_line(r) for r in results)
        lines.append(f"- {icon} **{name}** → `{signal}` 上榜数={list_count} 低位板块={low_ratio_txt or '无'}")
        lines.append(f"  - 退潮原因: {t['fade_reason']}")
        lines.append(f"  - 板块: {sector_txt}")
        lines.append(f"  - 处理后状态: **{status}** {note}")
        lines.append("")

        if signal == "revive_candidate" and status in ("reviving", "revived"):
            pushes.append(f"🚀 **{name} 出现重启信号**\n> 板块重新上榜{list_count}只，趋势转好且涨股比处于低位分位（启动期特征），{name}可能重回主线候选。请关注明日16:00主线推理。")
        if status == "dropped":
            pushes.append(f"❌ **{name} 移出监控池**\n> 行业趋势破坏连续3天（板块跌破MA60且MA20向下），主线无重启可能，停止监控。")
        if status == "revived":
            pushes.append(f"✅ **{name} 已恢复转正**\n> 重启信号连续2天确认，行业趋势再次向上。建议明日16:00主线推理重走三层过滤纳入候选。")

    reconcile_decisions = reconcile_mainline(conn, today, dry_run=args.dry_run)

    # ── 转正主线（revived）板块信号渲染 ──
    # 17:30 复检只覆盖 watching/reviving；但 18:00 将 revived 判为 🅰️ 建池产候选，
    # 依赖本报告 sector_map 做 trend_broken 过滤——revived 不入报告会导致 18:00
    # 误判"17:30 报告解析失败"而降级 C 不产候选。
    # 故将 revived 主线板块信号一并渲染（只渲染不判，不 update_status/save_checkin，幂等安全）。
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT id, theme_name, related_sectors, fade_reason FROM faded_theme_watch "
                    "WHERE watch_status='revived' ORDER BY fade_date, id")
        revived_list = cur.fetchall()
    for rt in revived_list:
        name = rt["theme_name"]
        sectors = extract_sectors(rt["related_sectors"])
        sec_codes = [s["code"] for s in sectors if isinstance(s, dict) and s.get("code")]
        if not sec_codes:
            continue
        print(f"🔍 {name}（revived 转正，仅渲染板块信号供 18:00）")
        pinfo = sector_info_from_rank(conn, sec_codes, rank_meta, today) if rank_meta else {}
        results = []
        for code in sec_codes:
            rows = get_sector_kline(code, today)
            r = analyze_sector(rows)
            if r:
                r["code"], r["name"] = code, next((s.get("name", "") for s in sectors if isinstance(s, dict) and s.get("code") == code), "")
                pi = pinfo.get(code, {})
                r["ratio"] = pi.get("ratio")
                r["ratio_today"] = pi.get("ratio_today")
                r["rank_daily"] = pi.get("rank_daily")
                r["rank_d20"] = pi.get("rank_d20")
                r["lb"] = pi.get("lb")
                r["strong"] = is_strong_sector(r, cross_section_count, quantiles)
                results.append(r)
        sector_txt = "; ".join(_sector_line(r) for r in results)
        # 主线行格式必须与 watching 行兼容（18:00 load_theme_report 正则：→ `(\w+)` 上榜数=(\d+)）
        list_count = sum(plate_t2.get(c, 0) for c in sec_codes)
        lines.append(f"- ✅ **{name}** → `revived` 上榜数={list_count} 低位板块=无")
        lines.append(f"  - 退潮原因: {rt['fade_reason']}")
        lines.append(f"  - 板块: {sector_txt}")
        lines.append(f"  - 处理后状态: **revived**（仅供 18:00 A级建池板块信号）")
        lines.append("")

    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT w.theme_name, w.watch_status, w.reconcile, w.reconcile_days, c.signal_type "
            "FROM faded_theme_watch w "
            "LEFT JOIN faded_theme_watch_checkins c "
            "ON c.theme_name=w.theme_name AND c.check_date=%s "
            "WHERE w.reconcile IN ('mainline_conflict','mainline_aligned') ORDER BY w.id",
            (today,))
        reconcile_rows = cur.fetchall()
    conflict_rows = [row for row in reconcile_rows if row["reconcile"] == "mainline_conflict"]
    aligned_rows = [row for row in reconcile_rows if row["reconcile"] == "mainline_aligned"]
    if conflict_rows:
        lines.append(f"## ⚠️ 同名冲突（{len(conflict_rows)} 条，判据方向相反）")
        for row in conflict_rows:
            lines.append(f"- **{row['theme_name']}**：16:00 主线池 active/peaking（信号层）vs 17:30 {row['signal_type']}（指数层）；取主线口径为准")
        lines.append("")
    if aligned_rows:
        lines.append(f"## ✅ 同步主线（{len(aligned_rows)} 条，判据方向一致）")
        for row in aligned_rows:
            lines.append(f"- **{row['theme_name']}**：本池 {row['signal_type']} 与主线池 active/peaking 方向一致，候选照常产出（见 18:00）")
        lines.append("")

    conn.close()
    report = "\n".join(lines)
    report_path = os.path.join(BASE_DIR, "data", f"faded_theme_watch_{label}.md")
    if args.dry_run:
        print("\n(dry-run 仅计算，不写库/不写报告/不推送)")
        return
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n✅ 监控报告: {report_path}")

    for p in pushes:
        push_wecom(p)


if __name__ == "__main__":
    main()
