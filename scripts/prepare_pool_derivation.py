#!/usr/bin/env python3
"""池外主营推导数据准备（设计 v2 3.1）——候选板块→代表股→主营文本/产品构成→推导输入 JSON
复用 outside_pool_themes（opt）的 load_latest_rank_snapshot / load_pool_codes。
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date
from typing import List, Dict, Optional

import outside_pool_themes as opt

# ================= 路径配置 =================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
DATA_DIR = os.path.join(WORKSPACE, "data")
CACHE_DIR = os.path.join(DATA_DIR, "business_cache")

# ================= 参数 =================
Q_ZDF = 0.85             # 分位参数，20 交易日均值校准，非市场常量
Q_RANK = 0.50            # 分位参数，20 交易日均值校准，非市场常量
Q_ZGB = 0.85             # 分位参数，20 交易日均值校准，非市场常量
STOCKS_TOP_N = 5         # 代表股：流通市值 TOP5
STOCKS_HOT_N = 3         # 交叉：当日涨幅 TOP3
CACHE_TTL_DAYS = 7       # 主营季度低频，缓存 7 天
REQUEST_INTERVAL = 0.3   # 每股请求间隔（防 429）
MAX_FETCH_PER_DAY = 100  # 批量补拉单日上限（与 18:30 上限口径一致）


def _to_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _within_ttl(fetched_at: str, ttl_days: int) -> bool:
    """检查缓存 fetched_at 是否在 TTL 内"""
    if not fetched_at:
        return False
    try:
        fetched = datetime.strptime(fetched_at, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (date.today() - fetched).days <= ttl_days


def get_url(url: str, timeout: int = 10) -> str:
    """urllib.request 获取 URL 内容，异常返回空字符串"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        print(f"  ⚠️ 请求失败 {url[:80]}: {e}", file=sys.stderr)
        return ""


def get_board_stocks(board_code: str) -> List[dict]:
    """获取板块成分股（rank_list），返回 [{code, name, ltsz, zdf, lb, zxj}]"""
    url = (f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
           f"?board_code={board_code}&sort_type=priceRatio&direct=down&count=200")
    raw = get_url(url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if data.get("code") != 0 or "data" not in data:
        return []
    rank_list = data["data"].get("rank_list", [])
    result = []
    for item in rank_list:
        if not isinstance(item, dict):
            continue
        result.append({
            "code": item.get("code", ""),
            "name": item.get("name", ""),
            "ltsz": _to_float(item.get("ltsz")),
            "zdf": _to_float(item.get("zdf")),
            "lb": _to_float(item.get("lb")),
            "zxj": _to_float(item.get("zxj")),
        })
    return result


def get_stock_business(code: str) -> dict:
    """获取公司主营业务（带缓存）。
    读缓存（CACHE_DIR/<code>.json，fetched_at 距今 ≤TTL 则复用）→ 未命中则 jiankuang。
    返回 {code, name, yw, report_date, products:[{name,value}], _from_cache:bool}；失败 note=错误信息。
    """
    cache_path = os.path.join(CACHE_DIR, f"{code}.json")

    # 尝试读缓存
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if _within_ttl(cached.get("fetched_at", ""), CACHE_TTL_DAYS):
                report_date = cached.get("report_date", "")
                return {
                    "code": code,
                    "name": cached.get("name", ""),
                    "yw": cached.get("yw", ""),
                    "report_date": report_date or '未知',
                    "products": cached.get("products", []),
                    "_from_cache": True,
                }
        except (json.JSONDecodeError, OSError):
            pass
        # 缓存过期 → 删除重拉
        try:
            os.remove(cache_path)
        except OSError:
            pass

    # 未命中缓存，请求 jiankuang API
    url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/stockinfo/jiankuang?code={code}"
    raw = get_url(url)
    if not raw:
        return {"code": code, "name": "", "yw": "", "report_date": "", "products": [],
                "_from_cache": False, "note": "请求失败"}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"code": code, "name": "", "yw": "", "report_date": "", "products": [],
                "_from_cache": False, "note": "JSON解析失败"}

    if not isinstance(payload, dict) or payload.get("code", 0) != 0:
        return {"code": code, "name": "", "yw": "", "report_date": "", "products": [],
                "_from_cache": False, "note": "接口返回错误"}

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return {"code": code, "name": "", "yw": "", "report_date": "", "products": [],
                "_from_cache": False, "note": "数据结构异常"}

    # 公司主营业务文本
    gsjj = data.get("gsjj") or {}
    yw_text = (gsjj.get("yw") or "").strip() if isinstance(gsjj, dict) else ""
    stock_name = (gsjj.get("gsmz") or "").strip() if isinstance(gsjj, dict) else ""

    # 主营收入构成（产品维度）
    report_date = ""
    products = []
    zysr_list = data.get("zysr") or []
    if isinstance(zysr_list, list) and len(zysr_list) > 0:
        latest = zysr_list[0] if isinstance(zysr_list[0], dict) else {}
        report_date = latest.get("date", "") or ""
        sections = latest.get("detail") or []
        if isinstance(sections, list):
            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                if sec.get("type") == "product":
                    detail_items = sec.get("detail") or []
                    if isinstance(detail_items, list):
                        for item in detail_items:
                            if not isinstance(item, dict):
                                continue
                            products.append({
                                "name": item.get("name", ""),
                                "value": item.get("zb", ""),
                            })

    if not report_date:
        report_date = '未知'
    result = {
        "code": code,
        "name": stock_name,
        "yw": yw_text,
        "report_date": report_date,
        "products": products,
        "_from_cache": False,
    }
    if not yw_text and not products:
        result["note"] = "主营数据为空"

    # 写缓存（有数据才写）
    if yw_text or products:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            cache_data = {
                "name": stock_name,
                "yw": yw_text,
                "report_date": report_date,
                "products": products,
                "fetched_at": datetime.now().strftime("%Y-%m-%d"),
            }
            # report_date='未知' 的缓存标记需重拉（7天TTL到期自动重拉）
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"  ⚠️ 缓存写入失败 {code}: {e}", file=sys.stderr)

    return result


def select_candidate_boards(rank_snapshot: List[dict], pool_codes: set,
                             existing_themes: Optional[List[dict]] = None,
                             date_compact: Optional[str] = None) -> List[dict]:
    """候选板块：当日池外横截面分位准入，选择层不设数量上限。

    通道1：zdf 分位达 Q_ZDF；通道2：rank_d20 分位、zgb_ratio 分位达阈值且 zdf 非负。
    主线 related_sectors 中的池外板块强制纳入，用于 absorb_check。
    """
    outside = [r for r in rank_snapshot if r["code"] not in pool_codes]
    zdf_values = [r["zdf"] for r in outside if r.get("zdf") is not None]
    rank_values = [r["rank_d20"] for r in outside if r.get("rank_d20") is not None]
    zgb_values = [r["zgb_ratio"] for r in outside if r.get("zgb_ratio") is not None]

    dim_a = [r for r in outside
             if r.get("zdf") is not None
             and opt.percentile_rank(r["zdf"], zdf_values) >= Q_ZDF]

    # 维度B：低位扩散（rank_d20 为位次，升序分位高表示20日涨幅靠后）
    dim_b = [r for r in outside
             if r.get("rank_d20") is not None
             and r.get("zgb_ratio") is not None
             and r.get("zdf") is not None
             and opt.percentile_rank(r["rank_d20"], rank_values) >= Q_RANK
             and opt.percentile_rank(r["zgb_ratio"], zgb_values) >= Q_ZGB
             and r["zdf"] >= 0]

    if existing_themes is None:
        date_compact = date_compact or (sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d"))
        existing_themes = opt.load_industry_themes(
            datetime.strptime(date_compact, "%Y%m%d").strftime("%Y-%m-%d"))

    mainline_codes = set()
    for theme in existing_themes or []:
        try:
            related = json.loads(theme.get("related_sectors") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        for sector in related.get("sectors", []):
            if isinstance(sector, dict) and sector.get("code"):
                mainline_codes.add(sector["code"])

    candidates = []
    seen_codes = set()

    for s in dim_b:
        if s["code"] not in seen_codes:
            s["_source"] = "dim_b"
            s["_purpose"] = "absorb_check" if s["code"] in mainline_codes else "new_chain"
            candidates.append(s)
            seen_codes.add(s["code"])

    for s in dim_a:
        if s["code"] not in seen_codes:
            s["_source"] = "dim_a"
            s["_purpose"] = "absorb_check" if s["code"] in mainline_codes else "new_chain"
            candidates.append(s)
            seen_codes.add(s["code"])

    for s in outside:
        if s["code"] not in seen_codes and s["code"] in mainline_codes:
            s["_source"] = s.get("_source") or "mainline"
            s["_purpose"] = "absorb_check"
            candidates.append(s)
            seen_codes.add(s["code"])

    return candidates


def _board_rank_fields(b: dict) -> dict:
    """提取板块完整 rank 指标字段"""
    return {
        "code": b.get("code"),
        "name": b.get("name"),
        "date": b.get("date"),
        "rank_daily": b.get("rank_daily"),
        "rank_d20": b.get("rank_d20"),
        "zdf": b.get("zdf"),
        "zdf_d5": b.get("zdf_d5"),
        "zdf_d20": b.get("zdf_d20"),
        "zgb_ratio": b.get("zgb_ratio"),
        "lb": b.get("lb"),
        "_source": b.get("_source"),
        "_purpose": b.get("_purpose"),
    }


def fetch_codes_batch(codes: List[str], dry_run: bool = False) -> dict:
    """批量补拉主营缓存（P1-1）：输入股票列表 → 查缓存 → 未命中调用 get_stock_business() → 写缓存。

    - 单次 ≤MAX_FETCH_PER_DAY 保护（超限截断并提示）
    - 补拉仅缓存操作，不触发任何 DB 写
    - dry_run=True 时只报告缓存命中/新增，不实际请求 API
    返回 {total, cache_hits, api_calls, fetched, skipped, truncated}。
    """
    codes = list(dict.fromkeys(codes))  # 去重保序
    truncated = False
    if len(codes) > MAX_FETCH_PER_DAY:
        print(f"  ⚠️ 输入 {len(codes)} 只超过单日上限 {MAX_FETCH_PER_DAY}，截断为前 {MAX_FETCH_PER_DAY} 只",
              file=sys.stderr)
        codes = codes[:MAX_FETCH_PER_DAY]
        truncated = True

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_hits = 0
    api_calls = 0
    fetched = 0
    skipped = 0

    for i, code in enumerate(codes):
        cache_path = os.path.join(CACHE_DIR, f"{code}.json")
        # 检查缓存是否在 TTL 内
        if os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as f:
                    cached = json.load(f)
                if _within_ttl(cached.get("fetched_at", ""), CACHE_TTL_DAYS):
                    cache_hits += 1
                    if dry_run:
                        print(f"  [{i+1}/{len(codes)}] {code}: 缓存命中（{cached.get('name','')}）")
                    continue
            except (json.JSONDecodeError, OSError):
                pass

        if dry_run:
            api_calls += 1
            print(f"  [{i+1}/{len(codes)}] {code}: 缓存未命中（dry-run 不请求 API）")
            continue

        biz = get_stock_business(code)
        if biz.get("_from_cache"):
            cache_hits += 1
        else:
            api_calls += 1
        if biz.get("yw") or biz.get("products"):
            fetched += 1
        else:
            skipped += 1
        if i < len(codes) - 1:
            time.sleep(REQUEST_INTERVAL)

    return {
        "total": len(codes),
        "cache_hits": cache_hits,
        "api_calls": api_calls,
        "fetched": fetched,
        "skipped": skipped,
        "truncated": truncated,
    }


def main():
    # P1-1: --fetch-codes / --fetch-list 批量补拉模式
    if "--fetch-codes" in sys.argv or "--fetch-list" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(description="主营缓存批量补拉（P1-1）")
        parser.add_argument("--fetch-codes", dest="fetch_codes",
                            help="逗号分隔股票代码（如 sh600001,sz000001）")
        parser.add_argument("--fetch-list", dest="fetch_list",
                            help="股票代码列表文件（每行一个代码）")
        parser.add_argument("--dry-run", action="store_true",
                            help="只报告缓存命中/新增，不请求 API")
        args = parser.parse_args()

        codes = []
        if args.fetch_codes:
            codes = [c.strip() for c in args.fetch_codes.split(",") if c.strip()]
        if args.fetch_list:
            with open(args.fetch_list, encoding="utf-8") as f:
                codes.extend(line.strip() for line in f if line.strip())

        if not codes:
            print("❌ 未提供股票代码（--fetch-codes 或 --fetch-list）", file=sys.stderr)
            sys.exit(1)

        print(f"📊 批量补拉：输入 {len(codes)} 只，上限 {MAX_FETCH_PER_DAY}/日", file=sys.stderr)
        stats = fetch_codes_batch(codes, dry_run=args.dry_run)
        mode = "dry-run" if args.dry_run else "实际拉取"
        print(f"✅ 补拉完成（{mode}）：总计 {stats['total']}，"
              f"缓存命中 {stats['cache_hits']}，API请求 {stats['api_calls']}"
              f"{'（已截断）' if stats['truncated'] else ''}", file=sys.stderr)
        return

    if len(sys.argv) < 2:
        print("用法: python3 prepare_pool_derivation.py <YYYYMMDD>", file=sys.stderr)
        print("      python3 prepare_pool_derivation.py --fetch-codes sh600001,sz000001 [--dry-run]",
              file=sys.stderr)
        sys.exit(1)
    date_compact = sys.argv[1]
    print(f"📐 分位参数：Q_ZDF={Q_ZDF}，Q_RANK={Q_RANK}，Q_ZGB={Q_ZGB}；"
          "校准来源：分位参数，20 交易日均值校准，非市场常量", file=sys.stderr)

    # 1) 加载快照和监控池
    print("📊 加载全A排行快照...", file=sys.stderr)
    rank_snapshot = opt.load_latest_rank_snapshot()
    if not rank_snapshot:
        print("❌ sector_rank_daily 无数据（17:30 未落库）", file=sys.stderr)
        sys.exit(1)
    snap_date = rank_snapshot[0]["date"]
    print(f"  快照日期: {snap_date}，共 {len(rank_snapshot)} 个行业", file=sys.stderr)

    print("📋 加载监控池板块...", file=sys.stderr)
    pool_codes = opt.load_pool_codes()
    print(f"  监控池 {len(pool_codes)} 个板块代码", file=sys.stderr)
    print("📋 加载 industry_themes...", file=sys.stderr)
    existing_themes = opt.load_industry_themes(
        datetime.strptime(date_compact, "%Y%m%d").strftime("%Y-%m-%d"))
    print(f"  当日 {len(existing_themes)} 条主线", file=sys.stderr)

    # 2) 候选板块
    print("\n🏭 识别候选板块（维度A/B）...", file=sys.stderr)
    boards = select_candidate_boards(rank_snapshot, pool_codes, existing_themes, date_compact)
    print(f"  候选板块 {len(boards)} 个（选择层无上限 / 取数层有保护：MAX_FETCH_PER_DAY={MAX_FETCH_PER_DAY}）", file=sys.stderr)

    # 3) 每板块获取代表股 + 主营
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_hits = 0
    api_calls = 0
    business_hits = 0
    total_stocks = 0

    board_results = []
    for bi, b in enumerate(boards):
        board_code = b["code"]
        board_name = b["name"]
        print(f"\n  [{bi+1}/{len(boards)}] {board_name}({board_code})...", file=sys.stderr)

        stocks = get_board_stocks(board_code)
        if not stocks:
            print(f"    ⚠️ 成分股获取失败", file=sys.stderr)
            board_results.append({
                **_board_rank_fields(b),
                "dim": b.get("_source", ""),
                "stocks": [],
                "note": "成分股获取失败",
            })
            continue

        # 市值TOP5 ∪ 涨幅TOP3（去重）
        by_ltsz = sorted([s for s in stocks if s.get("ltsz") is not None],
                         key=lambda x: x["ltsz"], reverse=True)
        by_zdf = sorted([s for s in stocks if s.get("zdf") is not None],
                        key=lambda x: x["zdf"], reverse=True)

        top_stocks = []
        seen = set()
        for s in by_ltsz[:STOCKS_TOP_N]:
            if s["code"] not in seen:
                top_stocks.append(s)
                seen.add(s["code"])
        for s in by_zdf[:STOCKS_HOT_N]:
            if s["code"] not in seen:
                top_stocks.append(s)
                seen.add(s["code"])

        print(f"    成分股 {len(stocks)}，代表股 {len(top_stocks)}", file=sys.stderr)

        # 获取每股主营
        stock_results = []
        for si, s in enumerate(top_stocks):
            code = s["code"]
            biz = get_stock_business(code)
            if biz.get("_from_cache"):
                cache_hits += 1
            else:
                api_calls += 1
            if biz.get("yw"):
                business_hits += 1
            total_stocks += 1

            stock_entry = {
                "code": code,
                "name": biz.get("name") or s.get("name", ""),
                "yw": biz.get("yw", ""),
                "products": biz.get("products", []),
                "report_date": biz.get("report_date", ""),
            }
            if biz.get("note"):
                stock_entry["note"] = biz["note"]
            stock_results.append(stock_entry)

            if si < len(top_stocks) - 1:
                time.sleep(REQUEST_INTERVAL)

        board_results.append({
            **_board_rank_fields(b),
            "dim": b.get("_source", ""),
            "stocks": stock_results,
        })

    # 4) 落盘 JSON
    output = {
        "date": date_compact,
        "snap_date": snap_date,
        "boards": board_results,
    }
    output_file = os.path.join(DATA_DIR, f"pool_derivation_input_{date_compact}.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 5) stderr 统计 + stdout 路径
    hit_rate = (business_hits / total_stocks * 100) if total_stocks else 0
    print(f"\n📊 统计：板块 {len(boards)}，代表股 {total_stocks}，"
          f"主营命中 {business_hits}({hit_rate:.0f}%)，缓存命中 {cache_hits}，API请求 {api_calls}",
          file=sys.stderr)
    print(output_file)


if __name__ == "__main__":
    main()
