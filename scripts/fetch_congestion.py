#!/usr/bin/env python3
"""Fetch constituents for candidate sectors, compute avg turnover & est 成交额占比."""
import json, subprocess, re, pickle, sys

TOTAL_MKT_TURNOVER = 21400  # 亿元 (from market_breadth)

# candidate sectors: (code, label, side)
candidates = [
    ("pt02GN2190", "光通信", "right"),
    ("pt02GN2236", "光芯片", "right"),
    ("pt02GN2234", "算力租赁", "right"),
    ("pt02GN2357", "腾讯云概念", "right"),
    ("pt02GN2145", "F5G", "right"),
    ("pt01801156", "医疗服务", "left"),
    ("pt02251441", "CRO", "left"),
    ("pt01801736", "风电设备", "left"),
    ("pt01801737", "电池", "left"),
    ("pt01801054", "小金属", "left"),
    ("pt01801122", "装修装饰Ⅱ", "left"),
    ("pt01801231", "综合Ⅱ", "left"),
]

def call_mcp(args):
    # 使用函数式语法调用 mcporter
    # 将 args dict 转为函数式参数格式
    args_str = "{" + ",".join(f'{k}:"{v}"' if isinstance(v, str) else f'{k}:{v}' for k, v in args.items()) + "}"
    expr = f"finance_plugin.finance_plugin({args_str})"
    cmd = ["mcporter", expr, "--output", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"content": [{"text": r.stdout[:500]}]}

results = {}
for code, label, side in candidates:
    d = call_mcp({"data_type": "constituent", "code": code})
    text = d["content"][0]["text"] if d.get("content") else ""
    # parse constituent rows: columns include 流通市值, 成交额(成交量手), etc.
    # turnover = 成交量(手)*100 / 流通股? We have 流通市值 and 成交量(手) and 最新价.
    # 换手率 ≈ 成交额(元) / 流通市值(元). 成交额 = 成交量(手)*100*最新价
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        if re.search(r"股票代码|公司名称|---", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 24:
            try:
                scode = cells[0]; name = cells[1]
                price = float(cells[9]); chg = float(cells[10])
                vol_hand = float(cells[11])  # 成交量手
                float_mv = float(cells[13])  # 流通市值 亿元
                # 成交额 亿元 = vol_hand*100*price / 1e8
                turnover_amt = vol_hand * 100 * price / 1e8  # 亿元
                turnover_rate = (turnover_amt / float_mv * 100) if float_mv > 0 else 0
                rows.append(dict(code=scode, name=name, price=price, chg=chg,
                                 vol_hand=vol_hand, float_mv=float_mv,
                                 turnover_amt=turnover_amt, turnover_rate=turnover_rate))
            except (ValueError, IndexError):
                continue
    if rows:
        avg_tr = sum(r["turnover_rate"] for r in rows) / len(rows)
        total_tr_amt = sum(r["turnover_amt"] for r in rows)
        jz_ratio = total_tr_amt / TOTAL_MKT_TURNOVER * 100
        results[code] = dict(label=label, side=side, n=len(rows),
                             avg_turnover=avg_tr, total_turnover_amt=total_tr_amt,
                             jz_ratio=jz_ratio, top=rows[:5])
        print(f"{label}({code}) [{side}] n={len(rows)} 平均换手={avg_tr:.2f}% 成交额占比≈{jz_ratio:.2f}% 板块成交={total_tr_amt:.1f}亿")
    else:
        print(f"{label}({code}) [{side}] NO ROWS PARSED")

with open("data/congestion_computed.pkl", "wb") as f:
    pickle.dump(results, f)
print("\nSaved congestion_computed.pkl")
