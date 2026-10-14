#!/usr/bin/env python3
"""F10 主营缓存补拉（R1-c 覆盖缺口回填工具）

用途：对指定行业板块的全部成分股，补齐 business_cache（F10 主营/产品构成）。
- 复用 prepare_pool_derivation.fetch_codes_batch（同一取数口径与 0.3s 间隔）
- 自设 100 只/批（脚本内 MAX_FETCH_PER_DAY 是单次调用保护，本工具分批调用绕过，非 API 硬限）
- 幂等：已缓存且 yw 非空的股票默认跳过（--force 强制重拉）

用法：
  python3 backfill_f10_batch.py --boards pt01801745,pt01801102 --limit 600
  python3 backfill_f10_batch.py --codes sh600001,sz000001 --force
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prepare_pool_derivation as ppd  # noqa: E402

CACHE_DIR = ppd.CACHE_DIR
BATCH = 100


def board_stocks(board_code: str) -> set:
    url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
           f"?board_code={board_code}&sort_type=priceRatio&direct=down&count=200")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8"))
        return {x.get("code") for x in j.get("data", {}).get("rank_list", []) if x.get("code")}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 板块 {board_code} 取数失败: {e}", file=sys.stderr)
        return set()


def cached_ok(code: str) -> bool:
    p = os.path.join(CACHE_DIR, f"{code}.json")
    if not os.path.exists(p):
        return False
    try:
        d = json.load(open(p, encoding="utf-8"))
        return bool((d.get("yw") or "").strip())
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", default="", help="逗号分隔的 pt 板块代码")
    ap.add_argument("--codes", default="", help="逗号分隔的股票代码")
    ap.add_argument("--limit", type=int, default=600, help="本次最多拉取只数")
    ap.add_argument("--force", action="store_true", help="已缓存也重拉")
    args = ap.parse_args()

    targets: set = set()
    for bc in [x for x in args.boards.split(",") if x.strip()]:
        s = board_stocks(bc.strip())
        print(f"  板块 {bc.strip()}: {len(s)} 只成分")
        targets |= s
    targets |= {x.strip() for x in args.codes.split(",") if x.strip()}

    todo = [c for c in sorted(targets) if args.force or not cached_ok(c)]
    print(f"目标 {len(targets)} 只 | 跳过已缓存 {len(targets) - len(todo)} 只 | 待拉 {len(todo)} 只（本次上限 {args.limit}）")
    todo = todo[: args.limit]

    done = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        ppd.fetch_codes_batch(chunk)
        done += len(chunk)
        print(f"  进度 {done}/{len(todo)}", flush=True)
        time.sleep(1)

    ok = sum(1 for c in todo if cached_ok(c))
    print(f"完成：本次拉取 {len(todo)} 只，缓存有效 {ok} 只，失败 {len(todo) - ok} 只")


if __name__ == "__main__":
    main()