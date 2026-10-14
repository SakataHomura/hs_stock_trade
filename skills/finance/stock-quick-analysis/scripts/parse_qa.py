#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量持仓快速分析的一次性解析脚本（替代逐个 read_file/sed/grep）。

用法: python3 ~/.hermes/skills/finance/stock-quick-analysis/scripts/parse_qa.py <code>
前置: 该股数据已落盘 /tmp/qa/<code>_quotes.txt /tmp/qa/<code>_batch.txt /tmp/qa/<code>_{lhb,blocktrade,margintrade}.txt（由 fetch_all 循环生成）
输出: 紧凑摘要——K线最近28行 / 缠论分型笔 / SECURITY关键字段 / FUND_FLOW历史 / NEWS标题 / westock三源
注意: NEWS 标题里可能出现主线概念同名词汇（研报摘要），概念归属验证必须看 SECURITY 概念板块行，勿用本输出里的 NEWS 做概念命中判定。
"""
import re, sys, os

def get_block(txt, name):
    m = re.search(r'={5,}\s*\[%s\]\s*={5,}(.*?)(?=\n={5,}\s*\[|$)' % name, txt, re.S)
    return m.group(1) if m else ""

def main(code):
    q = f"/tmp/qa/{code}_quotes.txt"
    b = f"/tmp/qa/{code}_batch.txt"
    print("#" * 20, code, "#" * 20)
    if os.path.exists(q):
        qt = open(q, encoding='utf-8').read()
        klines = re.findall(r'^\| (\d{4}-\d{2}-\d{2}) \| ([\d.]+|--) \| ([\d.]+|--) \| ([+-]?[\d.]+)%? \| ([\d.]+|--) \| ([\d.]+|--) \| ([\d.]+|--) \| ([\d.]+|--) \| ([\d.]+|--) \| ([\d.]+|--) \| ([\d.]+|--) \| ([\d.]+|--) \| ([\d.]+|--) \| ([\d.]+|--) \|$', qt, re.M)
        print(f"--- K线 {len(klines)} 行 (最近28行) ---")
        for k in klines[-28:]:
            print(" | ".join(k))
        chl = re.findall(r'^\| (\d{4}-\d{2}-\d{2}) \| (顶分型|底分型|向上笔|向下笔) \| ([^|]+) \| ([^|]+) \|?([^|]*?)\|?$', qt, re.M)
        print("--- 缠论 ---")
        for c in chl[-25:]:
            print(" | ".join(x.strip() for x in c))
    if os.path.exists(b):
        bt = open(b, encoding='utf-8').read()
        sec = get_block(bt, "SECURITY")
        print("--- SECURITY ---")
        for line in sec.splitlines():
            if any(k in line for k in ['公司名称', '最新价', '涨跌幅', '量比', '换手率', '市盈率', '总市值', '流通市值', '所属板块', '二级行业', '涨停', '跌停']):
                print(line.strip()[:200])
        ff = get_block(bt, "FUND_FLOW")
        print("--- FUND_FLOW (历史行) ---")
        hist = re.findall(r'\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([+-]?\d+\.?\d*)\s*\|\s*([\d.]+)\s*\|', ff)
        for h in hist[-12:]:
            print(" | ".join(h))
        st = re.search(r'分析说明\s*\|\s*([^|]+)', ff)
        if st:
            print("分析说明:", st.group(1).strip()[:150])  # 注意: "连续N日净流出" 已含当日盘中/收盘数据
        nw = get_block(bt, "NEWS")
        print("--- NEWS ---")
        for t in re.findall(r'### \d+\.\s*(.+?)\n', nw)[:6]:
            print("*", t.strip()[:90])
    for w in ['lhb', 'blocktrade', 'margintrade']:
        p = f"/tmp/qa/{code}_{w}.txt"
        if os.path.exists(p):
            wt = open(p, encoding='utf-8').read()
            print(f"--- {w} ---")
            lines = [l for l in wt.splitlines() if l.strip() and '———' not in l and '示例' not in l]
            print("\n".join(lines[:40]))
    print()

if __name__ == '__main__':
    main(sys.argv[1])