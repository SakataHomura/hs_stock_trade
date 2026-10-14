#!/usr/bin/env python3
"""
系统化计算产业链/板块的"第X天"。

核心规则（2026-08-18）：
- 产业链在板块统计中首次出现上榜数≥3只才算第1天
- 板块排行数据有信号但个股未成规模 → 不计入天数
- 连续2~3天维持≥3只 → 确认启动

数据源：
  1. data/plate/plate_YYYYMMDD.md 表格1（概念板块TOP10成分股）— 按概念标签聚类
  2. data/plate/plate_YYYYMMDD.md 表格2（二级行业板块统计）— 按板块聚类

用法：
  python3 scripts/compute_theme_days.py --list-all              # 列出所有活跃板块天数
  python3 scripts/compute_theme_days.py --sector pt01801039     # 查看单个板块时间线
  python3 scripts/compute_theme_days.py --theme "超硬材料/培育钻石"  # 查看产业链时间线
  python3 scripts/compute_theme_days.py --themes                # 查看所有产业链
  python3 scripts/compute_theme_days.py --concept "培育钻石"     # 按概念标签聚类
  python3 scripts/compute_theme_days.py --concept-auto          # 自动从表格1提取概念标签聚类
"""

import re
import json
import sys
import os
from datetime import datetime, timedelta
from collections import defaultdict

PLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'plate')

# ── 板块名称映射 ──
SECTOR_NAMES = {
    "pt01801039": "非金属材料Ⅱ", "pt01801072": "通用设备", "pt01801081": "半导体",
    "pt01801086": "电子化学品Ⅱ", "pt01801082": "其他电子Ⅱ", "pt01801102": "通信设备",
    "pt01801084": "光学光电子", "pt01801078": "自动化设备", "pt01801093": "汽车零部件",
    "pt01801083": "元件", "pt01801055": "工业金属", "pt01801054": "小金属",
    "pt01801053": "贵金属", "pt01801156": "医疗服务", "pt01801151": "化学制药",
    "pt01801735": "光伏设备", "pt01801085": "消费电子", "pt01801074": "专用设备",
    "pt01801036": "塑料", "pt01801712": "玻璃玻纤", "pt01801051": "金属新材料",
    "pt01801017": "养殖业", "pt01801722": "装修装饰Ⅱ", "pt01801733": "其他电源设备Ⅱ",
    "pt01801181": "房地产开发", "pt01801183": "房地产服务", "pt01801742": "航空装备Ⅱ",
    "pt01801155": "中药Ⅱ", "pt01801203": "一般零售", "pt01801104": "软件开发",
    "pt01801738": "电网设备", "pt01801971": "环境治理", "pt01801032": "化学纤维",
    "pt01801163": "燃气Ⅱ", "pt01801034": "化学制品", "pt01801765": "广告营销",
    "pt01801112": "黑色家电", "pt01801995": "电视广播Ⅱ", "pt01801223": "通信服务",
    "pt01801103": "IT服务Ⅱ", "pt01801731": "电机Ⅱ", "pt01801726": "工程咨询服务Ⅱ",
    "pt01801745": "军工电子Ⅱ", "pt01801045": "特钢Ⅱ", "pt01801141": "包装印刷",
    "pt01801113": "小家电", "pt01801994": "教育", "pt01801153": "医疗器械",
    "pt01801016": "种植业", "pt01801142": "家居用品", "pt01801737": "电池",
    "pt01801101": "计算机设备", "pt01801044": "电力", "pt01801050": "工业金属",
    "pt01801052": "贵金属", "pt01801056": "能源金属", "pt01801096": "商用车",
    "pt01801038": "农化制品", "pt01801962": "油服工程", "pt01801020": "油服工程",
    "pt01801963": "炼化及贸易", "pt01801744": "航海装备Ⅱ", "pt01801972": "环保设备Ⅱ",
    "pt01801033": "化学原料", "pt01801723": "基础建设", "pt01801012": "煤炭开采",
    "pt01801116": "家电零部件Ⅱ", "pt01801724": "专业工程", "pt01801231": "专业服务",
}

# ── 产业链定义（概念标签 → 产业链名称） ──
# 基于概念标签聚类，而非板块分类
# 这些标签会从表格1的"所属概念板块"列中匹配
CONCEPT_THEME_MAP = {
    "超硬材料/培育钻石": ["培育钻石", "超硬材料", "金刚石"],
    "半导体产业链": ["半导体产业", "先进封装(Chiplet)", "高带宽存储器HBM", "存储器", "模拟芯片", "中芯国际概念"],
    "光通信产业链": ["光芯片", "共封装光模块(CPO)", "光通信", "光纤概念"],
    "机器人产业链": ["机器人", "人形机器人", "工业机器人", "自动化"],
    "AI算力产业链": ["英伟达概念", "算力", "AI服务器", "数据中心"],
    "新能源产业链": ["光伏", "锂电池", "新能源", "风电"],
    "低空经济": ["低空经济", "飞行汽车", "eVTOL"],
    "消费电子": ["消费电子", "折叠屏", "智能穿戴"],
    "汽车产业链": ["汽车零部件", "无人驾驶", "智能座舱", "华为汽车"],
    "军工": ["军工", "航天", "航空"],
}


def parse_plate_table2(filepath):
    """解析板块统计文件的表格2，返回 {code: count}"""
    if not os.path.exists(filepath):
        return {}
    with open(filepath, 'r') as f:
        content = f.read()
    table2_match = re.search(r'## 表格2.*?\n\|.*?\n\|.*?\n(.*?)(?=\n## |\Z)', content, re.DOTALL)
    if not table2_match:
        return {}
    table2_text = table2_match.group(1)
    result = {}
    for line in table2_text.strip().split('\n'):
        line = line.strip()
        if not line.startswith('|'):
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 6:
            continue
        try:
            code = parts[3].strip()
            count = int(parts[4].strip())
            result[code] = count
        except (ValueError, IndexError):
            continue
    return result


def parse_plate_table1(filepath):
    """
    解析板块统计文件的表格1，按概念标签提取股票。
    
    返回: {concept_tag: [(stock_name, stock_code), ...]}
    """
    if not os.path.exists(filepath):
        return {}
    with open(filepath, 'r') as f:
        content = f.read()
    
    # 找到表格1
    table1_match = re.search(r'## 表格1.*?\n\|.*?\n\|.*?\n(.*?)(?=\n## |\Z)', content, re.DOTALL)
    if not table1_match:
        return {}
    
    table1_text = table1_match.group(1)
    result = defaultdict(list)
    
    for line in table1_text.strip().split('\n'):
        line = line.strip()
        if not line.startswith('|'):
            continue
        parts = [p.strip() for p in line.split('|')]
        # Markdown表格: ['', '#', '股票名称', '股票代码', '最新价', '当日涨跌幅', '5日涨跌幅', '20日涨跌幅', '所属概念板块', '']
        if len(parts) < 9:
            continue
        try:
            name = parts[2].strip()
            code = parts[3].strip()
            concepts = parts[8].strip()
            # 按概念标签拆分
            for concept in re.split(r'[、,，]', concepts):
                concept = concept.strip()
                if concept:
                    result[concept].append((name, code))
        except (ValueError, IndexError):
            continue
    
    return dict(result)


def get_all_plate_files():
    """获取所有按日期排序的板块统计文件"""
    files = []
    for f in sorted(os.listdir(PLATE_DIR)):
        m = re.match(r'plate_(\d{8})\.md$', f)
        if m:
            files.append((m.group(1), os.path.join(PLATE_DIR, f)))
    return files


def compute_sector_timeline(code, min_count=3, max_gap_days=10):
    """计算指定板块的时间线（基于表格2）"""
    files = get_all_plate_files()
    timeline = []
    first_day = None
    current_day = 0
    consecutive_days = 0
    last_had_count = False
    last_ge3_date = None
    
    for date_str, filepath in files:
        table2 = parse_plate_table2(filepath)
        count = table2.get(code, 0)
        
        entry = {"date": date_str, "count": count, "day": None}
        
        if count >= min_count:
            if first_day is None:
                first_day = date_str
                current_day = 1
                entry["day"] = 1
                consecutive_days = 1
            else:
                prev_dt = datetime.strptime(last_ge3_date, "%Y%m%d")
                curr_dt = datetime.strptime(date_str, "%Y%m%d")
                gap = (curr_dt - prev_dt).days
                
                if gap <= max_gap_days and last_had_count:
                    current_day += 1
                    consecutive_days += 1
                    entry["day"] = current_day
                elif gap <= max_gap_days and not last_had_count:
                    current_day += 1
                    consecutive_days = 1
                    entry["day"] = current_day
                else:
                    current_day = 1
                    consecutive_days = 1
                    entry["day"] = 1
            
            last_had_count = True
            last_ge3_date = date_str
        else:
            last_had_count = False
        
        timeline.append(entry)
    
    status = "未启动"
    if first_day:
        if consecutive_days >= 3:
            status = "确认期"
        elif consecutive_days >= 2:
            status = "试探期→确认期"
        elif consecutive_days >= 1:
            status = "试探期"
    
    name = SECTOR_NAMES.get(code, code)
    
    return {
        "code": code, "name": name, "timeline": timeline,
        "first_day": first_day, "current_day": current_day if first_day else 0,
        "consecutive_days": consecutive_days, "status": status,
    }


def compute_concept_timeline(concept_tag, min_count=3, max_gap_days=10):
    """
    按概念标签计算时间线。
    
    从表格1解析每个日期中带有该概念标签的股票数。
    这是产业链天数计算的核心方式——基于概念标签聚类。
    
    max_gap_days: 最大允许间隔天数。超过此天数后重新从第1天开始计算。
    默认10天（约2个交易周）。
    """
    files = get_all_plate_files()
    timeline = []
    first_day = None
    current_day = 0
    consecutive_days = 0
    last_had_count = False
    last_ge3_date = None
    
    for date_str, filepath in files:
        table1 = parse_plate_table1(filepath)
        stocks = table1.get(concept_tag, [])
        count = len(stocks)
        
        entry = {"date": date_str, "count": count, "stocks": [s[0] for s in stocks], "day": None}
        
        if count >= min_count:
            if first_day is None:
                # 首次出现
                first_day = date_str
                current_day = 1
                entry["day"] = 1
                consecutive_days = 1
            else:
                # 检查与上次≥3只的间隔
                prev_dt = datetime.strptime(last_ge3_date, "%Y%m%d")
                curr_dt = datetime.strptime(date_str, "%Y%m%d")
                gap = (curr_dt - prev_dt).days
                
                if gap <= max_gap_days and last_had_count:
                    # 连续出现，天数递增
                    current_day += 1
                    consecutive_days += 1
                    entry["day"] = current_day
                elif gap <= max_gap_days and not last_had_count:
                    # 间隔几天后再次出现（中间有0的日期），天数递增
                    current_day += 1
                    consecutive_days = 1
                    entry["day"] = current_day
                else:
                    # 间隔太久，重新从第1天开始
                    current_day = 1
                    consecutive_days = 1
                    entry["day"] = 1
            
            last_had_count = True
            last_ge3_date = date_str
        else:
            last_had_count = False
        
        timeline.append(entry)
    
    status = "未启动"
    if first_day:
        if consecutive_days >= 3:
            status = "确认期"
        elif consecutive_days >= 2:
            status = "试探期→确认期"
        elif consecutive_days >= 1:
            status = "试探期"
    
    return {
        "concept": concept_tag, "timeline": timeline,
        "first_day": first_day, "current_day": current_day if first_day else 0,
        "consecutive_days": consecutive_days, "status": status,
    }


def compute_theme_by_concept(theme_name, concept_tags):
    """
    基于概念标签计算产业链天数。
    
    产业链的"第X天" = 产业链内所有概念标签中，最早达到上榜数≥3的日期开始计算。
    产业链每天的上榜数 = 产业链内所有概念标签的并集（去重股票数）。
    """
    files = get_all_plate_files()
    
    # 先计算每个概念标签的时间线
    concept_results = {}
    for tag in concept_tags:
        concept_results[tag] = compute_concept_timeline(tag)
    
    # 计算产业链整体时间线（去重）
    theme_timeline = []
    theme_day = 0
    theme_started = False
    earliest_first_day = None
    last_ge3_date = None
    
    for date_str, filepath in files:
        table1 = parse_plate_table1(filepath)
        
        # 收集该日期所有概念标签下的股票（去重）
        all_stocks = set()
        for tag in concept_tags:
            stocks = table1.get(tag, [])
            for s in stocks:
                all_stocks.add(s[1])
        
        count = len(all_stocks)
        has_ge3 = count >= 3
        
        entry = {"date": date_str, "count": count, "has_ge3": has_ge3, "day": None}
        
        if has_ge3:
            if not theme_started:
                theme_started = True
                theme_day = 1
                entry["day"] = 1
                earliest_first_day = date_str
            else:
                # 检查间隔
                prev_dt = datetime.strptime(last_ge3_date, "%Y%m%d")
                curr_dt = datetime.strptime(date_str, "%Y%m%d")
                gap = (curr_dt - prev_dt).days
                
                if gap <= 10:
                    theme_day += 1
                    entry["day"] = theme_day
                else:
                    # 间隔太久，重新从第1天开始
                    theme_day = 1
                    entry["day"] = 1
            
            last_ge3_date = date_str
        
        theme_timeline.append(entry)
    
    return {
        "theme_name": theme_name,
        "concept_tags": concept_tags,
        "concept_results": concept_results,
        "theme_timeline": theme_timeline,
        "first_day": earliest_first_day,
        "current_day": theme_day if theme_started else 0,
        "started": theme_started,
    }


def auto_discover_concept_themes(min_count=3):
    """
    自动从最新板块统计文件表格1中提取概念标签，按产业链聚类。
    
    返回: {theme_name: [concept_tags]}
    """
    files = get_all_plate_files()
    latest_file = files[-1][1] if files else None
    if not latest_file:
        return {}
    
    table1 = parse_plate_table1(latest_file)
    
    # 统计每个概念标签的上榜数
    concept_counts = {}
    for tag, stocks in table1.items():
        concept_counts[tag] = len(stocks)
    
    # 按产业链聚类
    theme_concepts = defaultdict(list)
    for tag, count in sorted(concept_counts.items(), key=lambda x: -x[1]):
        if count < min_count:
            continue
        # 匹配产业链
        matched = False
        for theme_name, tags in CONCEPT_THEME_MAP.items():
            if tag in tags:
                theme_concepts[theme_name].append(tag)
                matched = True
                break
        if not matched:
            # 未匹配的标签归为"其他"
            theme_concepts["其他"].append(tag)
    
    return dict(theme_concepts)


def show_sector_detail(code):
    """显示单个板块的详细天数时间线"""
    result = compute_sector_timeline(code)
    name = SECTOR_NAMES.get(code, code)
    
    print(f"📊 {name}({code}) 天数时间线")
    print(f"   规则：板块统计中上榜数≥3只才算第1天")
    print()
    
    if result["first_day"]:
        print(f"   第1天: {result['first_day']}")
        print(f"   当前: 第{result['current_day']}天（连续{result['consecutive_days']}天）")
        print(f"   状态: {result['status']}")
    else:
        print(f"   状态: 未启动（从未达到上榜数≥3只）")
    
    print()
    print(f"{'日期':<12} {'上榜数':<8} {'天数':<6}")
    print("-" * 30)
    for entry in result["timeline"]:
        day_label = f"第{entry['day']}天" if entry["day"] else "-"
        print(f"{entry['date']:<12} {entry['count']:<8} {day_label:<6}")


def show_concept_detail(concept_tag):
    """显示单个概念标签的时间线"""
    result = compute_concept_timeline(concept_tag)
    
    print(f"📊 概念标签: {concept_tag}")
    print(f"   规则：板块统计表格1中该概念标签上榜数≥3只才算第1天")
    print()
    
    if result["first_day"]:
        print(f"   第1天: {result['first_day']}")
        print(f"   当前: 第{result['current_day']}天（连续{result['consecutive_days']}天）")
        print(f"   状态: {result['status']}")
    else:
        print(f"   状态: 未启动（从未达到上榜数≥3只）")
    
    print()
    print(f"{'日期':<12} {'上榜数':<8} {'天数':<10} {'股票'}")
    print("-" * 60)
    for entry in result["timeline"]:
        day_label = f"第{entry['day']}天" if entry["day"] else "-"
        stocks_str = ", ".join(entry["stocks"][:3])
        if len(entry["stocks"]) > 3:
            stocks_str += f"...(+{len(entry['stocks'])-3})"
        print(f"{entry['date']:<12} {entry['count']:<8} {day_label:<10} {stocks_str}")


def show_theme_detail(theme_name):
    """显示产业链的详细天数时间线"""
    tags = CONCEPT_THEME_MAP.get(theme_name, [])
    if not tags:
        print(f"未找到产业链: {theme_name}")
        return
    
    result = compute_theme_by_concept(theme_name, tags)
    
    print(f"📊 产业链: {theme_name}")
    print(f"   关联概念标签: {', '.join(tags)}")
    print()
    
    if result["started"]:
        print(f"   当前: 第{result['current_day']}天")
        print(f"   第1天: {result['first_day']}")
    else:
        print(f"   状态: 未启动")
    
    print()
    print(f"{'日期':<12} {'上榜数':<8} {'有≥3只':<10} {'天数':<6}")
    print("-" * 40)
    for entry in result["theme_timeline"]:
        day_label = f"第{entry['day']}天" if entry["day"] else "-"
        has_str = "✅" if entry["has_ge3"] else "❌"
        print(f"{entry['date']:<12} {entry['count']:<8} {has_str:<10} {day_label:<6}")
    
    print()
    print("各概念标签详情:")
    for tag, cr in result["concept_results"].items():
        day_str = f"第{cr['current_day']}天" if cr['current_day'] > 0 else "未启动"
        print(f"\n  {tag}: {day_str} (第1天: {cr['first_day'] or 'N/A'})")
        for entry in cr["timeline"]:
            if entry["day"]:
                print(f"    {entry['date']}: {entry['count']}只 第{entry['day']}天")


def list_all_sectors(min_count=3):
    """列出所有活跃板块及其天数"""
    files = get_all_plate_files()
    latest_file = files[-1][1] if files else None
    if not latest_file:
        print("没有找到板块统计文件")
        return
    
    latest_table2 = parse_plate_table2(latest_file)
    today = files[-1][0]
    
    print(f"📊 板块天数统计（基于 {today} 数据）")
    print(f"   规则：板块统计中上榜数≥{min_count}只才算第1天")
    print()
    
    results = []
    for code in latest_table2:
        result = compute_sector_timeline(code, min_count)
        if result["first_day"]:
            results.append(result)
    
    results.sort(key=lambda r: (-r["current_day"], -r["consecutive_days"]))
    
    print(f"{'板块名称':<14} {'代码':<18} {'第X天':<6} {'连续':<6} {'状态':<12} {'首次出现':<10}")
    print("-" * 70)
    for r in results:
        day_str = f"第{r['current_day']}天" if r['current_day'] > 0 else "未启动"
        print(f"{r['name']:<14} {r['code']:<18} {day_str:<6} {r['consecutive_days']:<6} {r['status']:<12} {r['first_day'] or 'N/A':<10}")


def list_all_concepts(min_count=3):
    """列出所有活跃概念标签及其天数"""
    files = get_all_plate_files()
    latest_file = files[-1][1] if files else None
    if not latest_file:
        print("没有找到板块统计文件")
        return
    
    latest_table1 = parse_plate_table1(latest_file)
    today = files[-1][0]
    
    print(f"📊 概念标签天数统计（基于 {today} 数据）")
    print(f"   规则：表格1中该概念标签上榜数≥{min_count}只才算第1天")
    print()
    
    results = []
    for tag in latest_table1:
        result = compute_concept_timeline(tag)
        if result["first_day"]:
            results.append(result)
    
    results.sort(key=lambda r: (-r["current_day"], -r["consecutive_days"]))
    
    print(f"{'概念标签':<20} {'第X天':<6} {'连续':<6} {'状态':<12} {'首次出现':<10}")
    print("-" * 60)
    for r in results:
        day_str = f"第{r['current_day']}天" if r['current_day'] > 0 else "未启动"
        print(f"{r['concept']:<20} {day_str:<6} {r['consecutive_days']:<6} {r['status']:<12} {r['first_day'] or 'N/A':<10}")


def list_all_themes(min_count=3):
    """列出所有产业链的天数"""
    print(f"📊 产业链天数统计（基于概念标签聚类）")
    print(f"   规则：产业链内概念标签去重后上榜数≥{min_count}只才算第1天")
    print()
    
    for theme_name, tags in sorted(CONCEPT_THEME_MAP.items()):
        result = compute_theme_by_concept(theme_name, tags)
        day_str = f"第{result['current_day']}天" if result['started'] else "未启动"
        print(f"\n{theme_name} — {day_str}")
        print(f"  概念标签: {', '.join(tags)}")
        if result['started']:
            print(f"  第1天: {result['first_day']}")
            print(f"  时间线:")
            for entry in result["theme_timeline"]:
                if entry["has_ge3"]:
                    day_label = f"第{entry['day']}天" if entry["day"] else ""
                    print(f"    {entry['date']}: 上榜{entry['count']}只 {day_label}")


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 scripts/compute_theme_days.py --list-all          # 所有板块天数")
        print("  python3 scripts/compute_theme_days.py --list-concepts     # 所有概念标签天数")
        print("  python3 scripts/compute_theme_days.py --sector pt01801039 # 单个板块")
        print("  python3 scripts/compute_theme_days.py --concept '培育钻石' # 单个概念标签")
        print("  python3 scripts/compute_theme_days.py --theme '超硬材料/培育钻石' # 产业链")
        print("  python3 scripts/compute_theme_days.py --themes            # 所有产业链")
        return
    
    if "--list-all" in sys.argv:
        list_all_sectors()
    elif "--list-concepts" in sys.argv:
        list_all_concepts()
    elif "--sector" in sys.argv:
        idx = sys.argv.index("--sector")
        if idx + 1 < len(sys.argv):
            show_sector_detail(sys.argv[idx + 1])
        else:
            print("请指定板块代码")
    elif "--concept" in sys.argv:
        idx = sys.argv.index("--concept")
        if idx + 1 < len(sys.argv):
            show_concept_detail(sys.argv[idx + 1])
        else:
            print("请指定概念标签名称")
    elif "--theme" in sys.argv:
        idx = sys.argv.index("--theme")
        if idx + 1 < len(sys.argv):
            show_theme_detail(sys.argv[idx + 1])
        else:
            print("请指定产业链名称")
    elif "--themes" in sys.argv:
        list_all_themes()


if __name__ == "__main__":
    main()
