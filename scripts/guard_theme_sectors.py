#!/usr/bin/env python3
"""sectors 联动守卫（默认 dry-run，--apply 仅写审计表）

对每条主线的 related_sectors 板块计算成分基线覆盖率分位，并结合
theme_constituents.primary_chain 的语义主导链输出审计结果。--apply 只写
portfolio.sector_link_audit，不修改 industry_themes。
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict

import pymysql

BASE = "https://proxy.finance.qq.com/cgi/cgi-bin/rank"
HEADERS = {"User-Agent": "Mozilla/5.0"}
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_CONFIG = dict(host="127.0.0.1", port=3306, user="wish", password="<DB_PASSWORD>",
                 database="portfolio", charset="utf8mb4")

Q_COV = 0.25
Q_DIL = 0.50

CREATE_AUDIT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sector_link_audit (
  id INT AUTO_INCREMENT PRIMARY KEY,
  trade_date DATE NOT NULL,
  link_name VARCHAR(128) NOT NULL,
  sector_code VARCHAR(32) NOT NULL,
  sector_name VARCHAR(128),
  verdict VARCHAR(32) NOT NULL,
  coverage_quantile DECIMAL(6,4) NULL,
  evidence TEXT,
  suggestion VARCHAR(255),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_link_sector (trade_date, link_name, sector_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_cache = {}


def http_json(url):
    if url in _cache:
        return _cache[url]
    req = urllib.request.Request(url, headers=HEADERS)
    data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
    _cache[url] = data
    return data


def normalize(s):
    return (s or "").replace("（", "(").replace("）", ")").replace(" ", "").strip()


def fetch_board_stocks(code):
    url = f"{BASE}/hs/getBoardRankList?board_code={code}&sort_type=priceRatio&direct=down&count=200"
    try:
        rows = (http_json(url).get("data") or {}).get("rank_list") or []
        return {row.get("code") for row in rows if row.get("code")}
    except Exception:
        return set()


def load_family_baseline(theme_name, parent_theme):
    """链族成分基线；parent_theme 非空时取同族全部现行子链并集。"""
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            if parent_theme:
                cur.execute(
                    "SELECT tc.stock_code FROM theme_constituents tc "
                    "WHERE tc.is_current=1 AND tc.theme_name IN ("
                    "  SELECT DISTINCT it.theme_name FROM industry_themes it "
                    "  WHERE it.parent_theme=%s AND it.is_legacy=0)",
                    (parent_theme,))
            else:
                cur.execute(
                    "SELECT stock_code FROM theme_constituents "
                    "WHERE theme_name=%s AND is_current=1",
                    (theme_name,))
            return {row[0] for row in cur.fetchall() if row[0]}
    except Exception:
        return set()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_plate(date_str):
    """解析 plate 文件，返回 (table1_rows, table2_secs)。"""
    path = f"{BASE_DIR}/data/plate/plate_{date_str.replace('-', '')}.md"
    table1_rows, table2_secs = [], {}
    try:
        with open(path, encoding="utf-8") as file:
            content = file.read()
    except FileNotFoundError:
        print(f"[warn] plate 文件不存在: {path}", file=sys.stderr)
        return table1_rows, table2_secs

    for block, kind in [(re.search(r"## 表格1.*?(?=\n## |\Z)", content, re.S), 1),
                        (re.search(r"## 表格2.*?(?=\n## |\Z)", content, re.S), 2)]:
        if not block:
            continue
        for line in block.group(0).splitlines():
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 6 or set(parts[1:4]) & {"#", "股票名称", "板块名称"}:
                continue
            if kind == 1 and len(parts) >= 9 and re.match(r"^[sbj]h\d{6}$", parts[3]):
                table1_rows.append({
                    "code": parts[3],
                    "concepts": [concept.strip() for concept in parts[8].split("、") if concept.strip()],
                })
            elif kind == 2 and len(parts) >= 6 and re.match(r"^pt\d+$", parts[3]):
                table2_secs[parts[2]] = {
                    "code": parts[3],
                    "stocks": set(re.findall(r"([sbj]h\d{6})", parts[5])),
                }
    return table1_rows, table2_secs


def quantile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = q * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def percentile_rank(value, values):
    if not values:
        return 0.0
    less_count = sum(item < value for item in values)
    equal_count = sum(item == value for item in values)
    return (less_count + equal_count / 2) / len(values)


def semantic_argmax(stock_codes, primary_chains):
    counts = Counter()
    for stock_code in stock_codes:
        for chain in primary_chains.get(stock_code, ()):
            counts[chain] += 1
    if not counts:
        return None
    max_count = max(counts.values())
    return min(chain for chain, count in counts.items() if count == max_count)


def load_primary_chains():
    primary_chains = defaultdict(set)
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stock_code, primary_chain FROM theme_constituents "
                "WHERE is_current=1 AND primary_chain IS NOT NULL AND primary_chain <> ''")
            for stock_code, chain in cur.fetchall():
                primary_chains[stock_code].add(chain)
        return primary_chains
    finally:
        conn.close()


def coverage(stocks, baseline):
    return len(stocks & baseline) / len(stocks) if stocks else 0.0


def build_audit_records(rows, table2_secs, date_str):
    link_sector_pairs = []
    sector_stocks = {}
    baselines = {}
    related_sector_codes_by_chain = defaultdict(set)
    for row in rows:
        try:
            related = json.loads(row["related_sectors"])
        except Exception:
            continue
        sectors = [
            sector for sector in (related.get("sectors") or [])
            if isinstance(sector, dict) and sector.get("code")
        ]
        if not sectors:
            continue
        baselines[row["theme_name"]] = load_family_baseline(
            row["theme_name"], row.get("parent_theme"))
        for sector in sectors:
            sector_code = sector["code"]
            related_sector_codes_by_chain[row["theme_name"]].add(sector_code)
            if sector_code not in sector_stocks:
                sector_stocks[sector_code] = fetch_board_stocks(sector_code)
            link_sector_pairs.append((row, sector, sector_stocks[sector_code]))

    coverage_values = [
        coverage(stocks, baselines[row["theme_name"]])
        for row, _, stocks in link_sector_pairs
    ]
    coverage_threshold = quantile(coverage_values, Q_COV)
    dilution_threshold = quantile(coverage_values, Q_DIL)
    primary_chains = load_primary_chains()
    existing_chains = {row["theme_name"] for row in rows if row.get("theme_name")}
    audit_records = []

    for row, sector, stocks in link_sector_pairs:
        link_name = row["theme_name"]
        baseline = baselines[link_name]
        cov = coverage(stocks, baseline)
        argmax = semantic_argmax(stocks, primary_chains)
        evidence = {
            "coverage": round(cov, 6),
            "coverage_threshold_q_cov": round(coverage_threshold, 6),
            "dilution_threshold_q_dil": round(dilution_threshold, 6),
            "sector_stock_count": len(stocks),
            "baseline_stock_count": len(baseline),
            "intersection_count": len(stocks & baseline),
            "semantic_argmax": argmax,
            "semantic_argmax_exists": argmax in existing_chains if argmax else False,
        }
        if cov <= coverage_threshold:
            verdict = "insufficient_coverage"
            suggestion = "成分基线覆盖不足，不得据此判定归属；需补全 theme_constituents 后复核"
        elif argmax and argmax not in existing_chains:
            verdict = "orphan_chain_candidate"
            suggestion = f"语义主导链={argmax}，现有链族未收录 → 独立成链候选"
        elif argmax and sector["code"] not in related_sector_codes_by_chain[argmax]:
            verdict = "semantic_mismatch"
            suggestion = (
                f"板块主营语义主导链={argmax}，该链未收录本板块；"
                f"建议补入 {argmax} 或移出 {link_name}"
            )
        elif cov <= dilution_threshold:
            verdict = "dilution"
            suggestion = "覆盖率处于稀释分位，仅提示观察，不修改链路归属"
        else:
            verdict = "ok"
            suggestion = "覆盖率与语义一致性通过"
        audit_records.append({
            "trade_date": date_str,
            "link_name": link_name,
            "sector_code": sector["code"],
            "sector_name": sector.get("name"),
            "verdict": verdict,
            "coverage_quantile": round(percentile_rank(cov, coverage_values), 4),
            "evidence": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            "suggestion": suggestion,
        })

    return audit_records


def print_audit_records(audit_records):
    grouped = defaultdict(list)
    for record in audit_records:
        grouped[record["verdict"]].append(record)

    print("\n===== 审计统计 =====")
    for verdict in sorted(grouped):
        print(f"{verdict}: {len(grouped[verdict])}")
    print("\n===== 审计明细 =====")
    for verdict in sorted(grouped):
        print(f"\n[{verdict}]")
        for record in grouped[verdict]:
            coverage_quantile = record["coverage_quantile"]
            coverage_text = (
                f" | coverage_quantile={coverage_quantile:.4f}"
                if coverage_quantile is not None else ""
            )
            print(f"- {record['link_name']} -> {record['sector_name']}({record['sector_code']}){coverage_text}")
            print(f"  suggestion: {record['suggestion']}")
            print(f"  evidence: {record['evidence']}")


def write_audit_records(audit_records):
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_AUDIT_TABLE_SQL)
            trade_dates = sorted({record["trade_date"] for record in audit_records})
            for trade_date in trade_dates:
                cur.execute(
                    "DELETE FROM sector_link_audit WHERE trade_date=%s",
                    (trade_date,))
            for record in audit_records:
                cur.execute(
                    "INSERT INTO sector_link_audit "
                    "(trade_date, link_name, sector_code, sector_name, verdict, "
                    "coverage_quantile, evidence, suggestion) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (record["trade_date"], record["link_name"], record["sector_code"],
                     record["sector_name"], record["verdict"], record["coverage_quantile"],
                     record["evidence"], record["suggestion"]))
        conn.commit()
        print(f"\n[apply] sector_link_audit 覆盖式写入 {len(audit_records)} 条（幂等）")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD，默认最新有记录交易日")
    parser.add_argument("--apply", action="store_true", help="仅写 sector_link_audit 审计表")
    args = parser.parse_args()

    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            if args.date:
                cur.execute(
                    "SELECT theme_name, parent_theme, theme_date, status, related_sectors "
                    "FROM industry_themes WHERE theme_date=%s AND is_legacy=0",
                    (args.date,))
            else:
                cur.execute(
                    "SELECT theme_name, parent_theme, theme_date, status, related_sectors "
                    "FROM industry_themes t "
                    "JOIN (SELECT MAX(theme_date) md FROM industry_themes) m "
                    "ON t.theme_date=m.md WHERE t.is_legacy=0")
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"[error] {args.date or '最新日期'}无 industry_themes 记录")
        sys.exit(1)

    date_str = str(rows[0]["theme_date"])
    table1_rows, table2_secs = load_plate(date_str)
    print(f"# sectors 联动守卫（{date_str}）mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print("判定源=theme_constituents.primary_chain；判据=覆盖率分位+语义一致性")
    print(f"plate 表格1 {len(table1_rows)} 行, 表格2 {len(table2_secs)} 板块\n")

    audit_records = build_audit_records(rows, table2_secs, date_str)
    print_audit_records(audit_records)
    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 仅写入 sector_link_audit 审计表。")
        return
    write_audit_records(audit_records)


if __name__ == "__main__":
    main()
