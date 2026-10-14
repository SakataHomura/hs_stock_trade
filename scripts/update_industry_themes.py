#!/usr/bin/env python3
"""
产业主线状态更新脚本（每日16:00执行）

功能：
1. 从今天板块统计文件表格1的概念标签，聚类识别今天活跃主线（新增）
   - 概念共现关系统计 → 并查集合并强关联概念 → 映射产业链 → 写入 industry_themes
2. 读取昨天的主线记录（从 MySQL industry_themes 表），对比今天板块统计文件表格2的上榜数据做状态迁移：
   - active → 今天上榜数维持 → 保持 active
   - active → 今天上榜数骤降 → 标记 peaking（观察期）
   - peaking → 今天继续下降 → 标记 fading（退潮观察期）
   - fading → 连续多天<3只 → 标记 ended（彻底退潮）
3. 新识别主线与迁移后的旧主线合并写入 MySQL：
   - 新识别主线若昨天已存在 → 走迁移保持连续天数
   - 迁移时若今天已写入同名主线 → 以新识别为准（概念并集上榜数）

用法：
  python3 scripts/update_industry_themes.py --date 2026-08-18
  python3 scripts/update_industry_themes.py  # 默认今天
"""

import re
import json
import sys
import os
from datetime import datetime, timedelta
from collections import defaultdict

try:
    import pymysql
except ImportError:
    print("需要安装 pymysql: pip install pymysql")
    sys.exit(1)

# ── MySQL 配置 ──
DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "portfolio",
    "charset": "utf8mb4",
}

PLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'plate')

# ── 板块名称映射（与 compute_theme_days.py 保持一致） ──
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
    "pt01801012": "农产品加工", "pt01801128": "休闲食品", "pt01801014": "饲料",
    "pt01801015": "渔业", "pt01801018": "动物保健Ⅱ", "pt01801124": "食品加工",
    "pt01801152": "生物制品", "pt01801153": "医疗器械", "pt01801155": "中药Ⅱ",
    "pt01801156": "医疗服务",
}

# ── 退潮阈值配置 ──
FADING_THRESHOLD = 0.3  # 上榜数降至峰值的30%以下视为骤降
FADING_CONTINUOUS_DAYS = 3  # 连续N天<3只视为彻底退潮
MIN_LISTING = 3  # 上榜数≥3只才算有信号

# ── 概念标签 → 产业链/主线 映射词典 ──
# 表格1的"所属概念板块"标签归并到产业链级主线，保证主线名跨日稳定
THEME_MAP = {
    # 煤化工产业链
    "煤化工概念": "煤化工产业链", "石墨电极": "煤化工产业链",
    "甲醇": "煤化工产业链", "焦煤概念": "煤化工产业链",
    # 煤炭产业链
    "煤炭概念": "煤炭产业链",
    # 石化产业链
    "油价相关": "石化产业链", "主营聚乙烯": "石化产业链",
    "PTA": "石化产业链", "醋酸": "石化产业链",
    "乙二醇": "石化产业链",
    # 农化产业链
    "草甘膦": "农化产业链",
    # 港口航运产业链
    "港口概念": "港口航运产业链", "波罗的海干散货指数(BDI)": "港口航运产业链",
    # 河南国改产业链
    "河南国企改革": "河南国改产业链", "河南自贸区": "河南国改产业链",
    "高速公路概念": "河南国改产业链", "光伏高速公路": "河南国改产业链",
    # 银行产业链
    "银行概念": "银行产业链", "证金持股": "银行产业链",
    # 医药产业链
    "独家药品概念": "医药产业链", "青蒿素": "医药产业链",
    # 机场航运产业链
    "机场概念": "机场航运产业链",
}

# 新识别主线最小规模：去重股票数 < 该值不视为主线（过滤噪声小簇）
MIN_THEME_STOCKS = 5
# 概念共现阈值：两个概念共同覆盖≥N只股票才认为强相关（同属一条主线）
CONCEPT_COOCCUR_THRESHOLD = 2


def get_db():
    """获取数据库连接（自动提交）"""
    conn = pymysql.connect(**DB_CONFIG)
    conn.autocommit(True)
    return conn


def parse_plate_table2(filepath):
    """解析板块统计文件表格2：二级行业板块统计"""
    if not os.path.exists(filepath):
        return {}
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 找到表格2
    table2_match = re.search(r'## 表格2：二级行业板块统计.*?\n(.+?)(?=\n##|\Z)', content, re.DOTALL)
    if not table2_match:
        return {}
    
    table2_text = table2_match.group(1)
    
    result = {}
    # 匹配表格行：| 序号 | 板块名称 | 板块代码 | 出现次数 | 股票列表 |
    pattern = r'\|\s*\d+\s*\|\s*([^|]+?)\s*\|\s*(pt\d+)\s*\|\s*(\d+)\s*\|'
    for m in re.finditer(pattern, table2_text):
        name = m.group(1).strip()
        code = m.group(2).strip()
        count = int(m.group(3))
        result[code] = {"name": name, "count": count}
    
    return result


def parse_plate_table1(filepath):
    """
    解析板块统计文件表格1：概念板块上榜明细
    
    返回:
        stocks: {code: {"name": str, "tags": [概念标签], "chg5": 5日涨跌幅}}
        concept_stocks: {概念: [code, ...]}  概念→股票 反向索引
    """
    if not os.path.exists(filepath):
        return {}, {}
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    table1_match = re.search(r'## 表格1.*?\n(.+?)(?=\n##|\Z)', content, re.DOTALL)
    if not table1_match:
        return {}, {}
    
    stocks = {}
    concept_stocks = defaultdict(list)
    for line in table1_match.group(1).strip().split('\n'):
        if not line.startswith('|'):
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 9:
            continue
        code = parts[3]
        try:
            chg5 = float(parts[6].replace('+', '').replace('%', ''))
        except (ValueError, IndexError):
            chg5 = 0.0
        tags = [t.strip() for t in re.split(r'[、,，]', parts[8]) if t.strip()]
        stocks[code] = {"name": parts[2], "tags": tags, "chg5": chg5}
        for t in tags:
            concept_stocks[t].append(code)
    return stocks, concept_stocks


def discover_new_themes(stocks, concept_stocks):
    """
    从表格1概念标签识别活跃主线：共现聚类 → 产业链归并
    
    算法：
    1. 概念标签做共现关系统计（两概念共同覆盖≥CONCEPT_COOCCUR_THRESHOLD只股票）
    2. 并查集合并强关联概念 → 概念簇
    3. 簇内概念映射到产业链（THEME_MAP）→ 主线名
    4. 不同簇映射到同一产业链时合并（跨日名称稳定）
    5. 过滤：去重股票数 < MIN_THEME_STOCKS 的小簇剔除
    
    返回:
        [{theme_name, concepts, stock_count, avg_chg5, major_concept}]
    """
    if not concept_stocks:
        return []
    
    # 只保留覆盖≥MIN_LISTING只股票的概念（过滤噪声标签）
    tags = [t for t, s in concept_stocks.items() if len(s) >= MIN_LISTING]
    if not tags:
        return []
    
    tag_idx = {t: i for i, t in enumerate(tags)}
    n = len(tags)
    
    # 共现统计
    cooccur = defaultdict(int)
    for code, info in stocks.items():
        tl = sorted({t for t in info['tags'] if t in tag_idx})
        for i in range(len(tl)):
            for j in range(i + 1, len(tl)):
                a, b = tag_idx[tl[i]], tag_idx[tl[j]]
                cooccur[(min(a, b), max(a, b))] += 1
    
    # 并查集
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    
    for (a, b), cnt in cooccur.items():
        if cnt >= CONCEPT_COOCCUR_THRESHOLD:
            union(a, b)
    
    # 组簇
    clusters = defaultdict(list)
    for i, t in enumerate(tags):
        clusters[find(i)].append(t)
    
    # 簇 → 产业链归并
    theme_clusters = defaultdict(lambda: {
        "concepts": set(), "stocks": set(), "chg5_list": []
    })
    for ctags in clusters.values():
        # 簇内股票并集
        cstock = set()
        for t in ctags:
            cstock.update(concept_stocks[t])
        # 簇的主产业链 = 覆盖股票最多的概念对应的映射
        major_tag = max(ctags, key=lambda t: len(concept_stocks[t]))
        theme_name = THEME_MAP.get(major_tag, major_tag)
        tc = theme_clusters[theme_name]
        tc["concepts"].update(ctags)
        tc["stocks"].update(cstock)
        for c in cstock:
            tc["chg5_list"].append(stocks[c]['chg5'])
    
    # 汇总排序
    result = []
    for theme_name, tc in theme_clusters.items():
        if len(tc["stocks"]) < MIN_THEME_STOCKS:
            continue
        avg_chg5 = sum(tc["chg5_list"]) / len(tc["chg5_list"]) if tc["chg5_list"] else 0.0
        result.append({
            "theme_name": theme_name,
            "concepts": sorted(tc["concepts"]),
            "stock_count": len(tc["stocks"]),
            "avg_chg5": avg_chg5,
        })
    result.sort(key=lambda r: (-r["stock_count"], -r["avg_chg5"]))
    return result


def get_yesterday_themes(target_date):
    """从 MySQL 获取昨天的产业主线记录"""
    yesterday = target_date - timedelta(days=1)
    yesterday_str = yesterday.strftime('%Y-%m-%d')
    
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id, theme_name, theme_date, status, consecutive_days, "
                "related_sectors, peak_count, peak_date "
                "FROM industry_themes WHERE theme_date = %s ORDER BY id",
                (yesterday_str,)
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_existing_themes(target_date_str):
    """获取今天已写入的主线记录"""
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT theme_name FROM industry_themes WHERE theme_date = %s",
                (target_date_str,)
            )
            return {r['theme_name'] for r in cur.fetchall()}
    finally:
        conn.close()


def extract_sector_codes(related_sectors_json):
    """从 related_sectors JSON 中提取板块代码列表"""
    try:
        sectors = json.loads(related_sectors_json)
        codes = []
        # 支持两种格式
        if isinstance(sectors, dict):
            for key in ('core', 'follower', 'material', 'indirect', 'sectors', 'partial'):
                items = sectors.get(key, [])
                if isinstance(items, list):
                    for item in items:
                        m = re.search(r'pt\d+', str(item))
                        if m:
                            codes.append(m.group())
        elif isinstance(sectors, list):
            for item in sectors:
                m = re.search(r'pt\d+', str(item))
                if m:
                    codes.append(m.group())
        return list(set(codes))
    except:
        return []


def compute_peak_listing(theme, today_listing, today_concepts=None):
    """
    计算主线在今天的上榜数
    
    优先用概念维度（新识别主线的 related_sectors 存 concepts）：
    - 若无 today_concepts 或主题无 concepts → 回退板块代码维度
    - 概念维度：取主线所有概念覆盖股票的并集数（去重，符合主线强度语义）
    - 板块维度：取关联板块中上榜数最高的
    """
    # 概念维度（新格式主线）
    concepts = extract_concepts(theme.get('related_sectors', '{}'))
    if concepts and today_concepts:
        all_stocks = set()
        for c in concepts:
            all_stocks.update(today_concepts.get(c, []))
        if all_stocks:
            return len(all_stocks)
    
    # 板块代码维度（旧格式主线）
    codes = extract_sector_codes(theme.get('related_sectors', '{}'))
    if not codes:
        return 0
    return max((today_listing.get(c, {}).get('count', 0) for c in codes), default=0)


def extract_concepts(related_sectors_json):
    """从 related_sectors JSON 中提取概念标签列表"""
    try:
        sectors = json.loads(related_sectors_json)
        if isinstance(sectors, dict):
            return sectors.get('concepts', [])
    except:
        pass
    return []


def compute_theme_status(theme, today_peak, peak_count):
    """
    根据昨天状态和今天数据，计算新状态
    
    状态迁移规则：
    active + 上榜数维持(≥峰值30%) → active
    active + 上榜数骤降(<峰值30%) → peaking
    peaking + 上榜数继续下降 → fading
    peaking + 上榜数反弹 → active
    fading + 连续多天<3只 → ended
    fading + 上榜数反弹 → active
    ended → ended（不改变）
    """
    yesterday_status = theme['status']
    peak = peak_count  # 历史峰值
    
    if yesterday_status == 'ended':
        return 'ended', False
    
    # 计算峰值30%阈值
    threshold = max(peak * FADING_THRESHOLD, MIN_LISTING)
    
    if today_peak >= MIN_LISTING and today_peak >= threshold:
        # 上榜数维持或反弹
        if yesterday_status in ('peaking', 'fading'):
            return 'active', True  # 反弹恢复
        return 'active', False
    
    if today_peak < MIN_LISTING:
        # 上榜数不足3只
        if yesterday_status == 'active':
            return 'peaking', False
        elif yesterday_status == 'peaking':
            return 'fading', False
        elif yesterday_status == 'fading':
            return 'ended', False
        return 'fading', False
    
    # 上榜数下降但未跌破3只
    if yesterday_status == 'active':
        return 'peaking', True
    elif yesterday_status == 'peaking':
        return 'fading', True
    elif yesterday_status == 'fading':
        return 'fading', False
    
    return 'fading', False


def save_new_themes(target_date_str, new_themes, yesterday_themes=None):
    """
    将今天新识别的主线写入 MySQL
    
    related_sectors 格式: {"concepts": [...], "major_concept": str}
    - 用概念标签而非板块代码，因为新主线来自表格1概念聚类
    - 次日迁移时 compute_peak_listing 走概念维度（并集股票数）
    
    若主线昨天已存在（同名），跳过新识别，交给迁移逻辑保持连续天数。
    """
    yesterday_names = {t['theme_name'] for t in (yesterday_themes or [])}
    existing = get_existing_themes(target_date_str)
    conn = get_db()
    
    try:
        with conn.cursor() as cur:
            for theme in new_themes:
                name = theme['theme_name']
                if name in existing:
                    print(f"  ⏭️ 跳过 {name}：今天已写入")
                    continue
                if name in yesterday_names:
                    print(f"  ⏭️ 跳过 {name}：昨天已存在，走状态迁移")
                    continue
                
                related = {
                    "concepts": theme['concepts'],
                    "major_concept": theme['concepts'][0] if theme['concepts'] else "",
                }
                cur.execute(
                    """INSERT INTO industry_themes 
                    (theme_name, theme_date, status, consecutive_days, 
                     related_sectors, peak_count, peak_date, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())""",
                    (name, target_date_str, 'active', 1,
                     json.dumps(related, ensure_ascii=False),
                     theme['stock_count'], target_date_str)
                )
                print(f"  🆕 {name}: 新识别 (第1天, {theme['stock_count']}只, "
                      f"5日均涨{theme['avg_chg5']:+.1f}%) "
                      f"概念: {'/'.join(theme['concepts'])}")
    finally:
        conn.close()


def save_theme_migration(target_date_str, old_themes, today_listing, today_concepts=None):
    """
    将昨天主线的状态迁移结果写入 MySQL
    
    对每条昨天主线：
    - 如果状态变为 ended → 写入今天记录，状态=ended
    - 如果状态保持 active → 写入今天记录，延续天数+1
    - 如果状态变为 peaking/fading → 写入今天记录
    - 如果今天新识别的主线已包含该主线 → 以新识别为准，不覆盖
    """
    existing = get_existing_themes(target_date_str)
    conn = get_db()
    
    try:
        with conn.cursor() as cur:
            for theme in old_themes:
                name = theme['theme_name']
                
                # 如果今天已写入同名主线，跳过（新识别优先）
                if name in existing:
                    print(f"  ⏭️ 跳过 {name}：今天已写入新记录")
                    continue
                
                # 计算今天上榜数
                today_peak = compute_peak_listing(theme, today_listing, today_concepts)
                peak_count = theme['peak_count'] or 0
                
                # 更新峰值
                if today_peak > peak_count:
                    peak_count = today_peak
                
                # 计算新状态
                new_status, changed = compute_theme_status(theme, today_peak, peak_count)
                
                # 计算连续天数
                new_days = theme['consecutive_days'] or 1
                if new_status == 'active':
                    new_days += 1
                elif new_status in ('peaking', 'fading'):
                    new_days = max(1, new_days - 1) if changed else new_days + 1
                elif new_status == 'ended':
                    new_days = theme['consecutive_days'] or 1
                
                # 写入今天记录
                peak_date = theme.get('peak_date')
                if peak_count > (theme.get('peak_count') or 0):
                    peak_date = target_date_str
                
                cur.execute(
                    """INSERT INTO industry_themes 
                    (theme_name, theme_date, status, consecutive_days, 
                     related_sectors, peak_count, peak_date, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())""",
                    (name, target_date_str, new_status, new_days,
                     theme['related_sectors'], peak_count, peak_date)
                )
                
                status_icon = {'active': '🟢', 'peaking': '🟡', 'fading': '🔴', 'ended': '⚫'}
                icon = status_icon.get(new_status, '❓')
                print(f"  {icon} {name}: {theme['status']} → {new_status} "
                      f"(今天上榜{ today_peak}只, 峰值{peak_count}, 第{new_days}天)")
                
    finally:
        conn.close()


def main():
    # 解析日期
    if "--date" in sys.argv:
        idx = sys.argv.index("--date")
        target_date = datetime.strptime(sys.argv[idx + 1], '%Y-%m-%d')
    else:
        target_date = datetime.now()
    
    target_date_str = target_date.strftime('%Y-%m-%d')
    plate_file = os.path.join(PLATE_DIR, f"plate_{target_date.strftime('%Y%m%d')}.md")
    
    print(f"📊 产业主线状态更新 - {target_date_str}")
    print(f"   数据源: {plate_file}")
    print()
    
    # 第1步：读取今天板块统计（表格1概念 + 表格2板块）
    print("📥 [1/4] 读取今天板块统计数据...")
    today_listing = parse_plate_table2(plate_file)
    today_stocks, today_concepts = parse_plate_table1(plate_file)
    if not today_listing and not today_stocks:
        print(f"  ⚠️ 未找到板块统计文件: {plate_file}")
        print("  将仅基于昨天记录做状态迁移（无今天数据）")
    else:
        print(f"  ✅ 表格2: {len(today_listing)} 个板块 | 表格1: {len(today_stocks)} 只股票, {len(today_concepts)} 个概念")
    print()
    
    # 第2步：识别今天新主线（概念聚类）
    print("🔍 [2/4] 识别今天活跃主线（概念共现聚类）...")
    new_themes = discover_new_themes(today_stocks, today_concepts)
    if new_themes:
        print(f"  ✅ 识别到 {len(new_themes)} 条活跃主线:")
        for t in new_themes:
            print(f"    🆕 {t['theme_name']} — {t['stock_count']}只, 5日均涨{t['avg_chg5']:+.1f}%")
    else:
        print("  ℹ️ 今天无足够规模的概念主线")
    print()
    
    # 第3步：获取昨天主线并迁移
    print("📥 [3/4] 获取昨天产业主线记录...")
    yesterday_themes = get_yesterday_themes(target_date)
    if not yesterday_themes:
        print("  ℹ️ 昨天无主线记录，跳过状态迁移（仅新识别）")
    else:
        print(f"  ✅ 昨天有 {len(yesterday_themes)} 条主线:")
        for t in yesterday_themes:
            print(f"    {t['status']} {t['theme_name']}")
    print()
    
    # 第4步：写入今天记录（先新识别，再迁移，迁移跳过同名）
    print("💾 [4/4] 写入今天主线记录...")
    if new_themes:
        print("  保存新识别主线:")
        save_new_themes(target_date_str, new_themes, yesterday_themes)
        print()
    if yesterday_themes:
        print("  旧主线状态迁移:")
        save_theme_migration(target_date_str, yesterday_themes, today_listing, today_concepts)
        print()
    
    # 汇总
    print("📋 更新汇总")
    conn = get_db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT theme_name, status, consecutive_days, peak_count "
                "FROM industry_themes WHERE theme_date = %s ORDER BY id",
                (target_date_str,)
            )
            new_records = cur.fetchall()
    finally:
        conn.close()
    
    if new_records:
        print(f"  ✅ 今日共 {len(new_records)} 条主线记录")
        for r in new_records:
            status_icon = {'active': '🟢', 'peaking': '🟡', 'fading': '🔴', 'ended': '⚫'}
            icon = status_icon.get(r['status'], '❓')
            print(f"  {icon} {r['theme_name']} — {r['status']} (第{r['consecutive_days']}天, 峰值{r['peak_count']}只)")
    else:
        print("  ℹ️ 今日无主线记录")
    
    print()
    print("✅ 主线状态更新完成")


if __name__ == "__main__":
    main()
