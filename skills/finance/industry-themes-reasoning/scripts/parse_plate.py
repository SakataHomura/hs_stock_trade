#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plate 文件解析 + 主线簇统计 + 板块交集分布 + R1.3 规模实测 参考工具
用法（用 plain open() 读文件，避免 hermes read_file 同会话占位符问题）：
    python3 parse_plate.py /path/to/plate_YYYYMMDD.md
产出：
    1. 表格1 概念频次统计（概念: 股数）
    2. 表格2 板块清单（板块: pt代码, 出现次数）
    3. 按传入簇定义（改 CLUSTERS dict）统计：簇成员/去重股数/平均5日涨幅/概念交集
    4. 每簇 → 表格2 板块交集分布（R1.2 覆盖完整性反查）
    5. R1.3 校验：概念股数 <30 → sectors 总规模 ≤ 概念股数×5；≥30 → ≤ 概念股数×3 或 300（取更严）
       （板块成分规模用腾讯 getBoardRankList 实测，勿凭表格2 出现次数冒充）
要点（2026-09-15 实证陷阱）：
    - 表格2 行格式 `| # | 板块名称 | 板块代码 | 出现次数 | 股票列表 |`：板块代码在 parts[2]！
    - 表格2 股票列表 `、` 分隔，每项 "名称(sh代码)" 需 split('(')[0] 取名称
    - 表格1 概念分隔符可能是 `、` 或 `,` 混合：replace('、', ',').split(',')
    - 股票名称可能含全角字符（鲁泰Ａ=U+FF21）：解析不必过滤，pandas/csv 直接按列取
    - 去重/均涨用 python 精确计算，纯 Agent 心算易错
    - 表格2 板块数是动态的（2026-09-15 为 30 板块），不要假设固定值；解析后核对 == 文件内表格2 行数
"""
import re
import sys
import json
import subprocess


def parse_table1(lines):
    """表格1：概念板块 TOP10 成分股汇总。返回 [{'name','code','5d','20d','concepts':[...]}]"""
    stocks, in_t1 = [], False
    for ln in lines:
        if ln.startswith('## 表格1'):
            in_t1 = True
            continue
        if ln.startswith('## ') and in_t1:
            in_t1 = False
        if in_t1 and ln.startswith('|') and '股票名称' not in ln and '|--' not in ln:
            parts = [p.strip() for p in ln.strip('|').split('|')]
            if len(parts) >= 8 and re.fullmatch(r'\d+', parts[0]):
                stocks.append({
                    'name': parts[1], 'code': parts[2], '5d': parts[5], '20d': parts[6],
                    'concepts': [c.strip() for c in parts[7].replace('、', ',').split(',') if c.strip()],
                })
    return stocks


def parse_table2(lines):
    """表格2：二级行业板块统计。返回 {板块名: (pt代码, 出现次数, [股票名...])}"""
    t2, in_t2 = {}, False
    for ln in lines:
        if ln.startswith('## 表格2'):
            in_t2 = True
            continue
        if ln.startswith('## ') and in_t2:
            in_t2 = False
        if in_t2 and ln.startswith('|') and '板块代码' not in ln and '|--' not in ln:
            parts = [p.strip() for p in ln.strip('|').split('|')]
            # 列位：parts[0]=#序号 parts[1]=板块名称 parts[2]=板块代码 parts[3]=出现次数 parts[4]=股票列表
            if len(parts) >= 5 and re.fullmatch(r'\d+', parts[0]) and re.fullmatch(r'pt\d+', parts[2]):
                names = [x.split('(')[0] for x in parts[4].split('、')]
                t2[parts[1]] = (parts[2], int(parts[3]), names)
    return t2


def board_distribution(stocks, cluster_fn, t2):
    """某簇股票 → 表格2 板块交集分布 {板块名: (pt代码, 交集数, [股票名...])}"""
    dist = {}
    for bname, (code, _cnt, bstocks) in t2.items():
        hit = [s['name'] for s in stocks if s['name'] in bstocks and cluster_fn(s)]
        if hit:
            dist[bname] = (code, len(hit), hit)
    return dict(sorted(dist.items(), key=lambda kv: -kv[1][1]))


def avg_5d(stocks, names):
    vals = []
    for s in stocks:
        if s['name'] in names:
            vals.append(float(s['5d'].replace('+', '').replace('%', '')))
    return sum(vals) / len(vals) if vals else float('nan')


def board_size(pt_code):
    """腾讯 getBoardRankList?count=200 实测板块成分规模（R1.3 用，勿用表格2 出现次数冒充）"""
    url = (f"https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
           f"?board_code={pt_code}&sort_type=priceRatio&direct=down&count=200")
    try:
        out = subprocess.run(['curl', '-s', '--max-time', '15', url],
                             capture_output=True, text=True, timeout=20).stdout
        return len(json.loads(out).get('data', {}).get('rank_list', []))
    except Exception as e:
        return f"ERR {e}"


def main():
    path = sys.argv[1]
    lines = open(path, encoding='utf-8').read().splitlines()
    stocks = parse_table1(lines)
    t2 = parse_table2(lines)
    print(f"表格1 股票数: {len(stocks)}  表格2 板块数: {len(t2)}")

    # 1. 概念频次
    freq = {}
    for s in stocks:
        for c in s['concepts']:
            freq[c] = freq.get(c, 0) + 1
    print("\n== 概念频次 ==")
    for c, n in sorted(freq.items(), key=lambda kv: -kv[1]):
        print(f"  {c}: {n}")

    # 2. 表格2 板块
    print("\n== 表格2 板块 ==")
    for b, (code, cnt, _) in sorted(t2.items(), key=lambda kv: -kv[1][1]):
        print(f"  {b}: {code} count={cnt}")

    # 3. 簇统计（按日修改 CLUSTERS 定义）
    CLUSTERS = {
        # 例：电子材料链
        # '电子材料': ['覆铜板', '电子布', '电子树脂', '复合集流体(PET铜箔)', 'PTFE概念', '触摸屏概念'],
        # 例：半导体材料链
        # '半导体材料': ['存储器', '长鑫存储', '高带宽存储器HBM', '先进封装(Chiplet)', '中芯国际概念',
        #               '半导体材料概念', '碳基半导体', '光刻胶', '电子特气', '靶材'],
    }
    if not CLUSTERS:
        print("\n[提示] 在脚本 CLUSTERS dict 定义今日簇，然后查看簇统计/板块分布/R1.3 校验")
        return
    all_names = set()
    for cl, concepts in CLUSTERS.items():
        def cf(s, concepts=concepts):
            return any(c in s['concepts'] for c in concepts)
        members = [s for s in stocks if cf(s)]
        names = {s['name'] for s in members}
        all_names |= names
        print(f"\n== 簇 {cl}（{len(concepts)} 概念）: {len(members)} 只 ==")
        print(f"  去重股数: {len(names)}  平均5日: {avg_5d(stocks, names):+.2f}%")
        dist = board_distribution(stocks, cf, t2)
        print("  板块交集分布 (R1.2 覆盖反查):")
        for b, (code, n, bnames) in dist.items():
            print(f"    {b} {code} x{n}: {bnames}")
        print("  R1.3 规模克制校验 (候选 sectors 按产业知识挑选后逐个实测):")
        # 例：candidate_sectors = [('元件','pt01801083'), ('玻璃玻纤','pt01801712')]
        candidate_sectors = []
        if candidate_sectors:
            total = 0
            for b, code in candidate_sectors:
                sz = board_size(code)
                total += sz if isinstance(sz, int) else 0
                print(f"    {b} {code}: {sz} 只")
            cap = len(names) * 5 if len(names) < 30 else min(len(names) * 3, 300)
            print(f"  概念股 {len(names)} → R1.3 上限 {cap}；候选 sectors 总规模 {total} "
                  f"{'✓' if total <= cap else '✗ 超限（核心板块优先保留须披露）'}")


if __name__ == '__main__':
    main()