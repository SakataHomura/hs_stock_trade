import sys, json, urllib.request

def fetch(code):
    url = f"https://proxy.finance.qq.com/cgi/cgi-bin/stockinfoquery/kline/app/get?code={code}&ktype=day&fqtype=qfq&limit=240"
    data = json.loads(urllib.request.urlopen(url, timeout=20).read().decode())
    nodes = data.get("data", {}).get("nodes", [])
    rows = [(n["date"], float(n["last"]), float(n["volume"])) for n in nodes]
    return rows

def ma(vals, n):
    if len(vals) < n: return None
    return sum(vals[-n:]) / n

for code in sys.argv[1:]:
    rows = fetch(code)
    closes = [r[1] for r in rows]
    vols = [r[2] for r in rows]
    if not closes:
        print(f"{code}: no data"); continue
    last = closes[-1]
    ma5, ma10, ma20, ma55 = ma(closes,5), ma(closes,10), ma(closes,20), ma(closes,55)
    # recent 6 days
    recent = rows[-6:]
    v5 = ma(vols,5)
    print(f"=== {code} ===")
    print(f"最新收盘: {last:.2f}  MA5={ma5:.2f} MA10={ma10:.2f} MA20={ma20:.2f} MA55={ma55:.2f}" if ma55 else f"最新收盘:{last:.2f} MA5={ma5} MA10={ma10} MA20={ma20}")
    align = "多头排列✅" if (ma5 and ma10 and ma20 and ma55 and ma5>ma10>ma20>ma55) else "非完美多头"
    print(f"均线结构: {align}  价格vs MA10: {'上方' if last>ma10 else '下方'} ({(last/ma10-1)*100:+.1f}%)")
    print("近6日 收盘/量:")
    for d,c,v in recent:
        print(f"  {d}  {c:.2f}  量{v/1e6:.1f}万手 {'放量' if v>v5*1.15 else ('缩量' if v<v5*0.85 else '平量')}")
    print()
