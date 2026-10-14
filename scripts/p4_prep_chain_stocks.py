#!/usr/bin/env python3
"""P4 辅助2：按 13 宽链 sectors 板块归属聚合全量主营缓存股，输出主营原文矩阵（供 LLM 切割决策）
归属规则: business_cache 中股票若属于某宽链 sectors 板块（表格2 板块-股票映射）或 LLM种子/规则归因命中宽链
输出: data/p4_prep/p4_chain_stocks_full.json
"""
import json, glob, os, re, sys
from collections import defaultdict

BASE = '/home/agentuser/data/定时任务打包/data'
OUT = '/home/agentuser/data/定时任务打包/data/p4_prep'
os.makedirs(OUT, exist_ok=True)

import pymysql
conn = pymysql.connect(host='127.0.0.1', user='wish', password='<DB_PASSWORD>', database='portfolio', charset='utf8mb4')
cur = conn.cursor()

# 0) 13 宽链 sectors
cur.execute("SELECT theme_name, related_sectors FROM industry_themes WHERE theme_date='2026-10-09' ORDER BY theme_name")
wide_chains = {}
for name, rs in cur.fetchall():
    d = json.loads(rs) if rs else {}
    wide_chains[name] = {'sectors': d.get('sectors', []), 'concepts': d.get('concepts', [])}

# 1) 表格2 板块代码->股票 (10-08 + 10-09)
def load_plate_t2(datestr):
    txt = open(f'{BASE}/plate/plate_{datestr}.md', encoding='utf-8').read()
    lines = txt.split('\n'); start = None
    for i, ln in enumerate(lines):
        if '表格2：二级行业板块统计' in ln: start = i; break
    sec = {}
    for ln in lines[start:]:
        parts = [p.strip() for p in ln.strip().strip('|').split('|')]
        if len(parts) >= 5 and re.match(r'^pt\d+', parts[2]):
            stocks = []
            for item in parts[4].split('、'):
                if '(' in item and item.endswith(')'):
                    stocks.append(item.split('(')[1].rstrip(')'))
            sec[parts[2]] = stocks
    return sec

t2 = {}
for d in ['20261008', '20261009']:
    for k, v in load_plate_t2(d).items():
        t2.setdefault(k, []).extend(v)

# 板块名 -> 股票列表映射
sec_name_to_code = {}
for wname, w in wide_chains.items():
    for s in w['sectors']:
        sec_name_to_code[s['name']] = s['code']

# 2) 全部缓存 + LLM 种子
cache = {}
for f in glob.glob(f'{BASE}/business_cache/*.json'):
    code = os.path.basename(f).replace('.json', '')
    try:
        d = json.load(open(f))
        if d.get('yw') or d.get('products'):
            cache[code] = d
    except: pass

llm_seed = {}
for datestr in ['20261008', '20261009']:
    try:
        j = json.load(open(f'{BASE}/pool_derivation_result_{datestr}.json'))
    except: continue
    for b in j.get('boards', []):
        for s in b.get('stocks', []) or []:
            if s and s.get('code') and s.get('theme') and s.get('confidence') != 'low':
                llm_seed.setdefault(s['code'], []).append((s['theme'], s.get('confidence'), (s.get('note') or '')[:50]))

# 3) 规则归因
sys.path.insert(0, '/tmp')
from biz_rules import rule_attr

# 4) 构建板块 -> 股票全集（表格2 并集 + 缓存股按主营规则）
sec_stocks = defaultdict(set)
for code, biz in cache.items():
    yw = biz.get('yw') or ''
    if not yw: continue
    for wname, w in wide_chains.items():
        for s in w['sectors']:
            # 简单匹配：主营含板块名关键词（粗筛，切割决策时人工复核）
            pass

# 主归属：每只缓存股 → 所属宽链（规则归因命中 或 表格2 板块归属）
chain_stocks = defaultdict(dict)  # wname -> {code: {yw,report_date,products,llm,rule,bizcode}}
for code, biz in cache.items():
    yw = biz.get('yw') or ''
    products = biz.get('products') or []
    r, rel = rule_attr(yw, products) if yw else (None, [])
    seeds = llm_seed.get(code)
    # 归属判定：规则命中宽链 / 表格2 板块命中 / LLM 种子命中
    hit_chains = set()
    # a) 规则
    if r:
        for wname in wide_chains:
            if r == wname or (wname in (r or '')) or (r and wname.replace('产业链','') in r):
                hit_chains.add(wname)
    # b) LLM 种子
    if seeds:
        for st, conf, note in seeds:
            for wname in wide_chains:
                if st == wname or wname in st:
                    hit_chains.add(wname)
    # c) 表格2 板块归属: 板块名 -> 宽链
    for wname, w in wide_chains.items():
        secnames = {s['name'] for s in w['sectors']}
        for sn in secnames:
            scode = sec_name_to_code.get(sn)
            if scode and code in t2.get(scode, []):
                hit_chains.add(wname)
    # d) 主营文本板块关键词命中（粗）
    for wname, w in wide_chains.items():
        for s in w['sectors']:
            if s['name'].replace('Ⅱ','') in yw:
                hit_chains.add(wname)

    info = {
        'code': code,
        'name': biz.get('name', ''),
        'yw': yw[:120],
        'report_date': biz.get('report_date', ''),
        'products': [f"{p.get('name','')}:{p.get('value','')}" for p in products if p.get('name')][:6],
        'rule': [r, rel[:3]] if r else None,
        'llm_seed': seeds[0] if seeds else None,
    }
    for wname in hit_chains:
        chain_stocks[wname][code] = info

# 5) 输出
overview = {}
for wname, stocks in sorted(chain_stocks.items()):
    overview[wname] = {
        'n': len(stocks),
        'n_yw': sum(1 for i in stocks.values() if i['yw']),
        'n_rule': sum(1 for i in stocks.values() if i.get('rule')),
        'n_llm': sum(1 for i in stocks.values() if i.get('llm_seed')),
        'rule_top': sorted({i['rule'][0] for i in stocks.values() if i.get('rule')}),
    }
    print(f"[{wname}] 覆盖 {len(stocks)} 只 | yw {overview[wname]['n_yw']} | 规则归 {overview[wname]['n_rule']} | LLM {overview[wname]['n_llm']}")
    print(f"    规则分布: {overview[wname]['rule_top']}")

json.dump({'wide_chains': {k: {'sectors': [s['name'] for s in v['sectors']], 'concepts': v['concepts']} for k, v in wide_chains.items()},
           'chain_stocks': {k: v for k, v in chain_stocks.items()},
           'overview': overview},
          open(f'{OUT}/p4_chain_stocks_full.json', 'w'), ensure_ascii=False, indent=1)
print(f'\n保存 -> {OUT}/p4_chain_stocks_full.json')