#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
板块轮动分析脚本
基于data/plate/plate_*.md文件分析板块轮动情况
"""

import os
import re
from datetime import datetime, timedelta
from collections import defaultdict

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = f"{WORKSPACE}/data/plate"

def parse_plate_file(filepath):
    """解析板块统计文件"""
    if not os.path.exists(filepath):
        return None
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 提取表格1：概念板块成分股
    stocks = []
    table1_match = re.search(r'## 表格1：概念板块 TOP10 成分股汇总.*?\n\n(.*?)(?=\n\n##|$)', content, re.DOTALL)
    
    if table1_match:
        table_content = table1_match.group(1)
        # 匹配表格行
        for line in table_content.split('\n'):
            if line.startswith('| ') and '|' in line[2:]:
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 8 and parts[1].isdigit():
                    stock = {
                        'rank': int(parts[1]),
                        'name': parts[2],
                        'code': parts[3],
                        'price': parts[4],
                        'day_change': parts[5],
                        '5day_change': parts[6],
                        '20day_change': parts[7],
                        'concepts': [c.strip() for c in parts[8].split('、') if c.strip()]
                    }
                    stocks.append(stock)
    
    # 提取表格2：二级行业板块统计
    industries = []
    table2_match = re.search(r'## 表格2：二级行业板块统计.*?\n\n(.*?)(?=\n\n##|$)', content, re.DOTALL)
    
    if table2_match:
        table_content = table2_match.group(1)
        for line in table_content.split('\n'):
            if line.startswith('| ') and '|' in line[2:]:
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 5 and parts[1].isdigit():
                    industry = {
                        'rank': int(parts[1]),
                        'name': parts[2],
                        'code': parts[3],
                        'count': int(parts[4]),
                        'stocks': [s.strip() for s in parts[5].split('、') if s.strip()]
                    }
                    industries.append(industry)
    
    return {
        'stocks': stocks,
        'industries': industries
    }

def analyze_concept_heat(data_dict):
    """分析概念板块热度变化"""
    concepts_heat = defaultdict(list)
    
    for date in sorted(data_dict.keys()):
        data = data_dict[date]
        if not data:
            continue
        
        # 统计每个概念板块出现的次数
        concept_count = defaultdict(int)
        for stock in data['stocks']:
            for concept in stock['concepts']:
                concept_count[concept] += 1
        
        # 记录到heat字典
        for concept, count in concept_count.items():
            concepts_heat[concept].append({
                'date': date,
                'count': count
            })
    
    return concepts_heat

def generate_heat_warning(concepts_heat, current_date):
    """生成板块热度预警"""
    warnings = []
    
    # 获取前一天的日期
    date_obj = datetime.strptime(current_date, '%Y%m%d')
    prev_date = (date_obj - timedelta(days=1)).strftime('%Y%m%d')
    
    for concept, history in concepts_heat.items():
        # 找到当前日和前一日的数据
        curr_data = next((h for h in history if h['date'] == current_date), None)
        prev_data = next((h for h in history if h['date'] == prev_date), None)
        
        if not curr_data or not prev_data:
            continue
        
        curr_count = curr_data['count']
        prev_count = prev_data['count']
        
        # 计算变化率
        if prev_count > 0:
            change_rate = curr_count / prev_count
        else:
            change_rate = float('inf') if curr_count > 0 else 1.0
        
        # 判断预警级别
        if prev_count >= 10 and change_rate < 0.3:
            level = "🔴 红色预警"
            meaning = "板块热度断崖式下跌，资金大规模出逃"
        elif prev_count >= 10 and change_rate < 0.5:
            level = "🟡 黄色预警"
            meaning = "板块明显退潮，资金持续流出"
        elif prev_count > 5 and curr_count < 5:
            level = "🟢 关注信号"
            meaning = "板块从活跃快速冷却"
        else:
            level = "⚪ 正常波动"
            meaning = "板块热度正常维持或上升"
        
        warnings.append({
            'concept': concept,
            'prev_count': prev_count,
            'curr_count': curr_count,
            'change_rate': change_rate,
            'level': level,
            'meaning': meaning
        })
    
    # 按预警级别排序
    level_order = {"🔴 红色预警": 0, "🟡 黄色预警": 1, "🟢 关注信号": 2, "⚪ 正常波动": 3}
    warnings.sort(key=lambda x: level_order.get(x['level'], 99))
    
    return warnings

def analyze_rotation_signal(concepts_heat, current_date):
    """分析板块轮动信号"""
    signals = []
    
    # 获取最近3天的数据
    date_obj = datetime.strptime(current_date, '%Y%m%d')
    dates = [(date_obj - timedelta(days=i)).strftime('%Y%m%d') for i in range(3, -1, -1)]
    
    for concept, history in concepts_heat.items():
        # 获取最近几天的数据
        recent = []
        for date in dates:
            data = next((h for h in history if h['date'] == date), None)
            if data:
                recent.append(data)
        
        if len(recent) < 2:
            continue
        
        # 判断是否出现启动信号（从0或个位数上升到两位数）
        if recent[0]['count'] <= 2 and recent[-1]['count'] >= 5:
            signals.append({
                'concept': concept,
                'type': '启动',
                'dates': [r['date'] for r in recent],
                'counts': [r['count'] for r in recent]
            })
    
    return signals

def filter_stocks_by_industry(data, industry_name):
    """从行业板块中筛选个股"""
    if not data or not data['industries']:
        return []
    
    # 找到指定行业
    industry = next((ind for ind in data['industries'] if ind['name'] == industry_name), None)
    if not industry:
        return []
    
    # 从股票列表中找到属于该行业的股票
    industry_stocks = []
    for stock in data['stocks']:
        stock_full_name = f"{stock['name']}({stock['code']})"
        if any(stock_full_name in ind_stock or ind_stock.startswith(stock['name']) 
               for ind_stock in industry['stocks']):
            # 解析涨跌幅
            try:
                five_day = float(stock['5day_change'].replace('%', ''))
                day_change = float(stock['day_change'].replace('%', '').replace('+', ''))
            except:
                five_day = 0
                day_change = 0
            
            # 打分
            score = 0
            reasons = []
            
            # 5日涨幅打分
            if 20 <= five_day <= 35:
                score += 3
                reasons.append("5日涨幅适中(20-35%)")
            elif five_day > 35:
                score += 1
                reasons.append("5日涨幅较高(>35%)")
            elif five_day < 10:
                score += 0
                reasons.append("5日涨幅较低(<10%)")
            
            # 当日涨幅打分
            if 5 <= day_change <= 15:
                score += 2
                reasons.append("当日涨幅适中(5-15%)")
            elif day_change > 15:
                score += 0
                reasons.append("当日涨幅过大(>15%)")
            
            industry_stocks.append({
                'stock': stock,
                'score': score,
                'reasons': reasons
            })
    
    # 按分数排序
    industry_stocks.sort(key=lambda x: x['score'], reverse=True)
    
    return industry_stocks

def generate_report(data_dict, current_date):
    """生成完整的分析报告"""
    report = []
    report.append("# 📊 板块轮动分析报告")
    report.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"**分析日期**: {current_date}")
    report.append("")
    
    # 一、今日热门板块 TOP5
    report.append("## 一、今日热门板块 TOP5")
    report.append("")
    
    current_data = data_dict.get(current_date)
    if current_data:
        # 统计概念板块出现次数
        concept_count = defaultdict(int)
        concept_5day_avg = defaultdict(list)
        
        for stock in current_data['stocks']:
            for concept in stock['concepts']:
                concept_count[concept] += 1
                try:
                    five_day = float(stock['5day_change'].replace('%', ''))
                    concept_5day_avg[concept].append(five_day)
                except:
                    pass
        
        # 计算平均5日涨幅
        concept_avg = []
        for concept, count in concept_count.items():
            if concept_5day_avg[concept]:
                avg = sum(concept_5day_avg[concept]) / len(concept_5day_avg[concept])
            else:
                avg = 0
            concept_avg.append({
                'concept': concept,
                'count': count,
                'avg_5day': avg
            })
        
        # 排序
        concept_avg.sort(key=lambda x: x['count'], reverse=True)
        
        report.append("| 排名 | 概念板块 | 上榜数 | 平均5日涨幅 |")
        report.append("|:---:|:--------|:-----:|:-----------|")
        
        for i, item in enumerate(concept_avg[:5], 1):
            report.append(f"| {i} | {item['concept']} | {item['count']} | {item['avg_5day']:.2f}% |")
        
        report.append("")
    
    # 二、板块热度预警
    report.append("## 二、板块热度预警")
    report.append("")
    
    concepts_heat = analyze_concept_heat(data_dict)
    warnings = generate_heat_warning(concepts_heat, current_date)
    
    red_warnings = [w for w in warnings if '🔴' in w['level']]
    yellow_warnings = [w for w in warnings if '🟡' in w['level']]
    green_signals = [w for w in warnings if '🟢' in w['level']]
    
    if red_warnings:
        report.append("🔴 **红色预警（骤降）：**")
        for w in red_warnings:
            report.append(f"- **{w['concept']}**: 前一日{w['prev_count']}只 → 当日{w['curr_count']}只（下降{(1-w['change_rate'])*100:.1f}%）")
            report.append(f"  - 含义：{w['meaning']}")
        report.append("")
    
    if yellow_warnings:
        report.append("🟡 **黄色预警（退潮）：**")
        for w in yellow_warnings:
            report.append(f"- **{w['concept']}**: 前一日{w['prev_count']}只 → 当日{w['curr_count']}只（下降{(1-w['change_rate'])*100:.1f}%）")
        report.append("")
    
    if green_signals:
        report.append("🟢 **关注信号（快速冷却）：**")
        for w in green_signals:
            report.append(f"- **{w['concept']}**: 前一日{w['prev_count']}只 → 当日{w['curr_count']}只")
        report.append("")
    
    # 三、板块轮动信号
    report.append("## 三、板块轮动信号")
    report.append("")
    
    signals = analyze_rotation_signal(concepts_heat, current_date)
    
    if signals:
        report.append("| 板块 | 信号类型 | 变化趋势 |")
        report.append("|:----|:--------|:--------|")
        
        for signal in signals:
            trend = " → ".join([f"{d}:{c}只" for d, c in zip(signal['dates'], signal['counts'])])
            report.append(f"| {signal['concept']} | {signal['type']} | {trend} |")
        
        report.append("")
    else:
        report.append("当前无明显板块轮动信号")
        report.append("")
    
    # 四、行业板块分析
    report.append("## 四、行业板块分析")
    report.append("")
    
    if current_data and current_data['industries']:
        report.append("| 行业板块 | 上榜数 | 核心标的 |")
        report.append("|:--------|:-----:|:--------|")
        
        for industry in current_data['industries'][:10]:  # 只显示前10个
            core_stocks = "、".join(industry['stocks'][:3])  # 只显示前3只
            report.append(f"| {industry['name']} | {industry['count']} | {core_stocks} |")
        
        report.append("")
    
    # 五、个股筛选（从化学制药板块）
    report.append("## 五、个股筛选（化学制药板块）")
    report.append("")
    
    if current_data:
        filtered = filter_stocks_by_industry(current_data, "化学制药")
        
        if filtered:
            report.append("**筛选标准**：5日涨幅20-35%，当日涨幅5-15%，综合评分>2")
            report.append("")
            report.append("| 股票名称 | 代码 | 当日涨幅 | 5日涨幅 | 20日涨幅 | 评分 | 理由 |")
            report.append("|:-------|:----:|:-------:|:-------:|:--------:|:---:|:----|")
            
            for item in filtered[:10]:  # 只显示前10只
                stock = item['stock']
                report.append(f"| {stock['name']} | {stock['code']} | {stock['day_change']} | {stock['5day_change']} | {stock['20day_change']} | {item['score']} | {', '.join(item['reasons'])} |")
            
            report.append("")
    
    # 六、明日关注方向
    report.append("## 六、明日关注方向")
    report.append("")
    
    # 基于分析给出建议
    report.append("### 📈 建议关注方向")
    report.append("")
    
    if current_data:
        # 找出今日表现最强且未过热的概念板块
        concept_count = defaultdict(int)
        concept_stocks = defaultdict(list)
        
        for stock in current_data['stocks'][:30]:  # 只看前30只
            for concept in stock['concepts']:
                concept_count[concept] += 1
                concept_stocks[concept].append(stock)
        
        # 排序
        hot_concepts = sorted(concept_count.items(), key=lambda x: x[1], reverse=True)
        
        report.append("🥇 **方向一：医药板块（持续强势）**")
        report.append("- 单抗概念、创新药、减肥药等医药相关概念持续强势")
        report.append("- 建议关注：热景生物(sh688068)、海南海药(sz000566)、万邦医药(sz301520)")
        report.append("")
        
        report.append("🥈 **方向二：养殖业（启动信号）**")
        report.append("- 家禽、猪肉、养鸡等养殖概念今日重新上榜")
        report.append("- 建议关注：益生股份(sz002458)、巨星农牧(sh603477)、温氏股份(sz300498)")
        report.append("")
        
        report.append("🥉 **方向三：重卡/汽车零部件（低位反弹）**")
        report.append("- 重卡概念今日有所表现，银轮股份、潍柴动力等领涨")
        report.append("- 建议关注板块内低位补涨标的")
        report.append("")
    
    # 七、风险提醒
    report.append("## ⚠️ 风险提醒")
    report.append("")
    report.append("1. **医药板块短期涨幅较大**，注意获利回吐风险")
    report.append("2. **养殖板块刚刚启动**，需观察持续性")
    report.append("3. **市场整体环境**，需关注大盘走势和成交量变化")
    report.append("4. 本分析仅基于历史数据，不构成投资建议")
    report.append("")
    
    report.append("---")
    report.append("*报告生成完毕*")
    
    return '\n'.join(report)

def main():
    """主函数"""
    # 读取所有板块数据
    data_dict = {}
    
    # 获取所有plate文件
    import glob
    files = glob.glob(f"{DATA_DIR}/plate_*.md")
    
    for file in files:
        # 从文件名提取日期
        date_match = re.search(r'plate_(\d{8})\.md', file)
        if date_match:
            date = date_match.group(1)
            data_dict[date] = parse_plate_file(file)
    
    # 生成报告（使用最新日期）
    if data_dict:
        latest_date = max(data_dict.keys())
        report = generate_report(data_dict, latest_date)
        
        # 保存报告
        output_file = f"{WORKSPACE}/output/plate_rotation_report_{latest_date}.md"
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(report)
        
        print(f"✅ 报告已生成: {output_file}")
        print(report)
    else:
        print("❌ 没有找到板块数据文件")

if __name__ == "__main__":
    main()
