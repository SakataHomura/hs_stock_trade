#!/usr/bin/env python3
"""
第二步：PE过滤 + 策略分析（使用腾讯行情接口，无限频）

从 data/screened_YYYY-MM-DD.json 读取初筛结果
→ PE过滤（-100 < PE < 500）
→ 输出完整分析报告

用法：
  python3 scripts/stock_analysis.py [日期]
  默认日期：2026-07-10
"""
import subprocess
import time
import re
import sys
import json
import os
import requests

def query_pe_qt(code):
    """使用腾讯行情接口查询PE"""
    url = f"https://qt.gtimg.cn/?fmt=json&q={code}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        arr = data.get(code, [])
        if len(arr) >= 39:
            pe_str = arr[38]  # 索引38是PE
            if pe_str and pe_str != '-' and pe_str != '':
                return float(pe_str)
    except:
        pass
    return None

def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-07-10"
    input_file = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'data', f'screened_{date_str}.json'))
    
    # 读取初筛结果
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print("=" * 100)
    print(f"第二步：PE过滤 + 策略分析")
    print(f"日期：{date_str}")
    print(f"通过精筛的股票：{len(data['passed_stocks'])} 只")
    print("=" * 100)
    
    # PE过滤（使用腾讯行情接口，无限频）
    print(f"\n🔍 PE过滤（仅保留 -100 < PE < 500，使用腾讯行情接口）...")
    pe_results = []
    pe_failed = []
    
    for stock in data['passed_stocks']:
        code = stock['code']
        pe = query_pe_qt(code)
        if pe is not None and -100 < pe < 500:
            stock['pe'] = pe
            pe_results.append(stock)
            print(f"    ✅ {code} {stock['name']}: PE={pe:.0f} | 评分{stock['score']:.0f}分")
        elif pe is not None:
            pe_failed.append({**stock, 'pe': pe, 'reason': f'PE={pe:.0f}超出范围'})
            print(f"    ❌ {code} {stock['name']}: PE={pe:.0f}（超出范围）")
        else:
            pe_failed.append({**stock, 'pe': None, 'reason': 'PE数据缺失'})
            print(f"    ❌ {code} {stock['name']}: PE数据缺失")
    
    print(f"\n  通过PE过滤: {len(pe_results)} 只")
    print(f"  PE过滤失败: {len(pe_failed)} 只")
    
    if pe_failed:
        print(f"  过滤明细:")
        for s in sorted(pe_failed, key=lambda x: x['score'], reverse=True):
            print(f"    ❌ {s['code']} {s['name']}: 评分{s['score']:.0f}分 | {s['reason']}")
    
    # 输出完整报告
    print(f"\n{'=' * 100}")
    print("📊 完整分析报告")
    print("=" * 100)
    
    directions = {
        "存储芯片/半导体（含算力/AIGC）": "持续",
        "存储芯片产业链（纯存储）": "持续",
        "半导体全产业链": "持续",
    }
    
    for dir_name, dir_type in directions.items():
        print(f"\n{'─' * 60}")
        print(f"📌 {dir_name}（{dir_type}）")
        print(f"{'─' * 60}")
        
        dir_passed = [s for s in pe_results if dir_name in s['directions']]
        dir_failed = [s for s in pe_failed if dir_name in s['directions']]
        
        print(f"  通过PE过滤: {len(dir_passed)} 只")
        print(f"  PE过滤失败: {len(dir_failed)} 只")
        
        if dir_passed:
            print(f"\n  🏆 推荐排名（按评分降序）:")
            sorted_passed = sorted(dir_passed, key=lambda x: x['score'], reverse=True)
            for i, s in enumerate(sorted_passed[:10], 1):
                details = [f"评分{s['score']:.0f}分", f"PE={s['pe']:.0f}"]
                if s.get('detail'):
                    top_dims = sorted(s['detail'].items(), key=lambda x: x[1], reverse=True)[:3]
                    details.append(f"强项: {'/'.join([f'{k}={v:.0f}' for k,v in top_dims])}")
                print(f"    {i}. {s['code']} {s['name']}: {' | '.join(details)}")
    
    # 交叉标的汇总
    print(f"\n{'=' * 100}")
    print("🔀 跨方向交叉标的汇总（核心信号）")
    print(f"{'=' * 100}")
    
    cross_passed = [s for s in pe_results if len(s['directions']) > 1]
    cross_failed = [s for s in pe_failed if len(s['directions']) > 1]
    
    if cross_passed:
        print(f"\n  ✅ 通过PE过滤的交叉标的:")
        sorted_cross = sorted(cross_passed, key=lambda x: (len(x['directions']), x['score']), reverse=True)
        print(f"  {'股票':<25} {'方向数':<8} {'评分':<8} {'PE':<8} {'方向'}")
        print(f"  {'-'*25} {'-'*8} {'-'*8} {'-'*8} {'-'*40}")
        for s in sorted_cross:
            dirs_str = ', '.join(s['directions'])
            print(f"  {s['code']} {s['name']:<13} {len(s['directions']):<8} {s['score']:<8.0f} {s['pe']:<8.0f} {dirs_str}")
    else:
        print(f"\n  ❌ 无通过PE过滤的交叉标的")
    
    if cross_failed:
        print(f"\n  ❌ 被过滤的交叉标的:")
        for s in sorted(cross_failed, key=lambda x: len(x['directions']), reverse=True):
            print(f"    {s['code']} {s['name']}: 评分{s['score']:.0f}分 | {s['reason']}")
    
    # 全量推荐排名
    print(f"\n{'=' * 100}")
    print("🏆 全量推荐排名（所有方向合并，按评分降序）")
    print(f"{'=' * 100}")
    
    if pe_results:
        all_sorted = sorted(pe_results, key=lambda x: x['score'], reverse=True)
        print(f"\n  {'排名':<6} {'股票':<25} {'评分':<8} {'PE':<8} {'方向':<35} {'强项'}")
        print(f"  {'-'*6} {'-'*25} {'-'*8} {'-'*8} {'-'*35} {'-'*45}")
        for i, s in enumerate(all_sorted, 1):
            dirs_str = ', '.join(s['directions'])
            top_dims = sorted(s['detail'].items(), key=lambda x: x[1], reverse=True)[:3]
            dims_str = '/'.join([f"{k}={v:.0f}" for k,v in top_dims])
            cross_mark = " 🔀" if len(s['directions']) > 1 else ""
            print(f"  {i:<6} {s['code']} {s['name']:<13} {s['score']:<8.0f} {s['pe']:<8.0f} {dirs_str:<35} {dims_str}{cross_mark}")
    else:
        print(f"\n  无通过PE过滤的候选")
    
    # 统计汇总
    print(f"\n{'=' * 100}")
    print("📊 统计汇总")
    print(f"{'=' * 100}")
    
    print(f"\n  总候选股: {data['total_candidates']}")
    print(f"  精筛通过: {data['passed']}")
    print(f"  精筛否决: {data['vetoed']}")
    print(f"  PE过滤通过: {len(pe_results)}")
    print(f"  PE过滤失败: {len(pe_failed)}")
    print(f"  交叉标的（通过PE过滤）: {len(cross_passed)}")
    
    print(f"\n{'=' * 100}")
    print("分析完成")
    print(f"{'=' * 100}")


if __name__ == "__main__":
    main()
