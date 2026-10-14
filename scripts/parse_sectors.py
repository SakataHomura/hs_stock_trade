#!/usr/bin/env python3
"""Parse sector_rank batch JSON and score congestion candidates."""
import json, re, pickle

with open("data/sector_rank_20260814.json", encoding="utf-8") as f:
    data = json.load(f)
text = data["content"][0]["text"]

# Extract tag + following content by scanning lines
sections = {}  # tag -> content text
tag = None
buf = []
for line in text.splitlines():
    m = re.search(r'\[SECTOR_RANK_(\w+)_(\w+)\]', line)
    if m:
        if tag is not None:
            sections[tag] = "\n".join(buf)
        tag = (m.group(1), m.group(2))
        buf = []
    else:
        buf.append(line)
if tag is not None:
    sections[tag] = "\n".join(buf)

def parse_rows(section):
    out = {}
    for line in section.splitlines():
        if not line.startswith('|'):
            continue
        if re.search(r'排名/总数|板块名称|---', line):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) >= 19:
            try:
                rank_ok = cells[0].split('/')[0].isdigit()
            except Exception:
                rank_ok = False
            if not rank_ok:
                continue
            try:
                code = cells[2]
                out[code] = dict(name=cells[1], code=code,
                                 chg=float(cells[3]), chg5=float(cells[4]), chg20=float(cells[5]),
                                 vol_ratio=float(cells[13]), jz_ratio=float(cells[14]),
                                 main_net=float(cells[15]), main5=float(cells[16]), main20=float(cells[17]))
            except (ValueError, IndexError):
                continue
    return out

hy2_price = parse_rows(sections.get(("hy2","PriceRatioD20"), ""))
gn_price  = parse_rows(sections.get(("gn","PriceRatioD20"), ""))
hy2_fund  = parse_rows(sections.get(("hy2","NetMainInD5"), ""))
gn_fund   = parse_rows(sections.get(("gn","NetMainInD5"), ""))

def merge(d, fund):
    for code, v in fund.items():
        if code in d:
            d[code].update(main_net=v["main_net"], main5=v["main5"], main20=v["main20"])
merge(hy2_price, hy2_fund); merge(gn_price, gn_fund)

def congestion_state(jz):
    if jz > 45: return "高拥挤❌"
    if jz >= 35: return "中等拥挤⚠️"
    return "低拥挤✅"

print("=== 行业板块(hy2) — 20日涨幅>9% 候选 ===")
print(f"{'板块':<14}{'20日%':>8}{'5日%':>8}{'成交占比%':>10}{'量比':>6}{'拥挤度':>12}{'5日主力(亿)':>14}")
for code, v in sorted(hy2_price.items(), key=lambda x: -x[1]["chg20"]):
    if v["chg20"] > 9:
        print(f"{v['name']:<14}{v['chg20']:>8.2f}{v['chg5']:>8.2f}{v['jz_ratio']:>10.2f}{v['vol_ratio']:>6.2f}{congestion_state(v['jz_ratio']):>12}{v['main5']/1e4:>14.2f}")

print("\n=== 概念板块(gn) — 20日涨幅>11% 候选 ===")
print(f"{'板块':<16}{'20日%':>8}{'5日%':>8}{'成交占比%':>10}{'量比':>6}{'拥挤度':>12}{'5日主力(亿)':>14}")
for code, v in sorted(gn_price.items(), key=lambda x: -x[1]["chg20"]):
    if v["chg20"] > 11:
        print(f"{v['name']:<16}{v['chg20']:>8.2f}{v['chg5']:>8.2f}{v['jz_ratio']:>10.2f}{v['vol_ratio']:>6.2f}{congestion_state(v['jz_ratio']):>12}{v['main5']/1e4:>14.2f}")

with open("data/sectors_parsed.pkl","wb") as f:
    pickle.dump({"hy2":hy2_price,"gn":gn_price}, f)
print("\nParsed rows hy2:", len(hy2_price), "gn:", len(gn_price))
