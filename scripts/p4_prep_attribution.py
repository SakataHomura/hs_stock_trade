#!/usr/bin/env python3
"""P4 存量迁移辅助：聚合 13 宽链 sectors 成分股 + 主营归因数据源
输出: data/p4_prep/p4_attribution_matrix.json + 控制台概况
数据源: 10-09 industry_themes related_sectors / plate 表格2 / business_cache / LLM种子(10-08,10-09) / 规则引擎
"""
import json, glob, os, re, sys
from collections import defaultdict

BASE = '/home/agentuser/data/定时任务打包/data'
OUT = '/home/agentuser/data/定时任务打包/data/p4_prep'
os.makedirs(OUT, exist_ok=True)

import pymysql
conn = pymysql.connect(host='127.0.0.1', user='wish', password='<DB_PASSWORD>', database='portfolio', charset='utf8mb4')
cur = conn.cursor()

# 1) 13 宽链 sectors（10-09）
cur.execute("SELECT theme_name, related_sectors FROM industry_themes WHERE theme_date='2026-10-09' ORDER BY theme_name")
wide_chains = {}
for name, rs in cur.fetchall():
    d = json.loads(rs) if rs else {}
    wide_chains[name] = d.get('sectors', [])
print(f'[1] 13宽链 sectors: {len(wide_chains)} 条')
for n, s in wide_chains.items():
    print(f'    {n}: {[x["name"] for x in s]}')

# 2) plate 表格2 板块股票列表（10-08/10-09）-> 板块代码 -> [股票代码...]
def load_plate_t2(datestr):
    txt = open(f'{BASE}/plate/plate_{datestr}.md', encoding='utf-8').read()
    lines = txt.split('\n'); start = None
    for i, ln in enumerate(lines):
        if '表格2：二级行业板块统计' in ln: start = i; break
    sec = {}  # code -> stocks
    for ln in lines[start:]:
        parts = [p.strip() for p in ln.strip().strip('|').split('|')]
        if len(parts) >= 5 and re.match(r'^pt\d+', parts[2]):
            stocks = []
            for item in parts[4].split('、'):
                if '(' in item and item.endswith(')'):
                    code = item.split('(')[1].rstrip(')')
                    stocks.append(code)
            sec[parts[2]] = stocks
    return sec

t2_08 = load_plate_t2('20261008')
t2_09 = load_plate_t2('20261009')
print(f'[2] plate 表格2 板块数: 10-08={len(t2_08)} 10-09={len(t2_09)}')

# 3) business_cache 主营
cache = {}
for f in glob.glob(f'{BASE}/business_cache/*.json'):
    code = os.path.basename(f).replace('.json', '')
    try:
        d = json.load(open(f))
        cache[code] = d if (d.get('yw') or d.get('products')) else None
    except: pass
cache = {k: v for k, v in cache.items() if v}
print(f'[3] business_cache 有效主营: {len(cache)}')

# 4) LLM 种子（池外推导 stocks theme）
llm_seed = {}  # code -> (theme, conf, note)
for datestr in ['20261008', '20261009']:
    try:
        j = json.load(open(f'{BASE}/pool_derivation_result_{datestr}.json'))
    except Exception as e:
        print(f'    WARN {datestr} 解析失败: {e}'); continue
    for b in j.get('boards', []):
        bname = b.get('name'); bcode = b.get('code')
        for s in b.get('stocks', []) or []:
            if not s: continue
            code = s.get('code')
            if s.get('theme') and s.get('confidence') != 'low':
                llm_seed.setdefault(code, []).append((s.get('theme'), s.get('confidence'), (s.get('note') or '')[:40]))
print(f'[4] LLM 种子高置信归因: {len(llm_seed)} 只')

# 5) 规则归因（biz_rules.py rule_attr）
sys.path.insert(0, '/tmp')
try:
    from biz_rules import rule_attr
    rule_ok = True
except Exception as e:
    print(f'    WARN 规则引擎加载失败: {e}; 用内嵌简化版')
    rule_ok = False

def rule_attr_safe(yw, products=None):
    if rule_ok:
        return rule_attr(yw, products)
    return None, []

# 6) 聚合：13 宽链 sectors 板块 -> 成分股（表格2 两日并集 + 缓存/种子里的板块成员）
chain_stocks = {}   # wide_chain -> {code: info}
for wname, secs in wide_chains.items():
    codes = set()
    for s in secs:
        scode = s.get('code'); sname = s.get('name')
        if scode in t2_09: codes.update(t2_09[scode])
        if scode in t2_08: codes.update(t2_08[scode])
    chain_stocks[wname] = sorted(codes)
    print(f'    {wname}: 表格2 可覆盖成分 {len(codes)} 只 ({secs[0]["name"] if secs else "?"}...)')

# 7) 每股信息矩阵
matrix = {}
all_codes = sorted(set(c for cs in chain_stocks.values() for c in cs))
for code in all_codes:
    info = {'code': code}
    biz = cache.get(code)
    info['yw'] = (biz.get('yw') or '')[:80] if biz else ''
    info['report_date'] = biz.get('report_date', '') if biz else ''
    prods = (biz.get('products') or []) if biz else []
    info['products'] = [f"{p.get('name','')}" for p in prods if p.get('name')][:5]
    seeds = llm_seed.get(code)
    info['llm_seed'] = seeds[0] if seeds else None
    info['rule'] = None
    if biz and biz.get('yw'):
        r, rel = rule_attr_safe(biz.get('yw'), prods)
        if r:
            info['rule'] = [r, rel[:3]]
    matrix[code] = info

print(f'[7] 聚合矩阵: {len(matrix)} 只')
# 8) 覆盖统计
n_yw = sum(1 for i in matrix.values() if i['yw'])
n_seed = sum(1 for i in matrix.values() if i['llm_seed'])
n_rule = sum(1 for i in matrix.values() if i['rule'])
print(f'    主营 yw 覆盖: {n_yw}/{len(matrix)}; LLM种子: {n_seed}; 规则可归: {n_rule}')

out = {
    'wide_chains': {k: [s['name'] for s in v] for k, v in wide_chains.items()},
    'wide_chain_sectors': {k: v for k, v in wide_chains.items()},
    'chain_stocks': chain_stocks,
    'matrix': matrix,
    'meta': {'n_chains': len(wide_chains), 'n_stocks': len(matrix),
             'n_yw': n_yw, 'n_seed': n_seed, 'n_rule': n_rule,
             'cache_total': len(cache), 'llm_seed_total': len(llm_seed)}
}
json.dump(out, open(f'{OUT}/p4_attribution_matrix.json', 'w'), ensure_ascii=False, indent=1)
print(f'\n[8] 保存 -> {OUT}/p4_attribution_matrix.json')