#!/usr/bin/env python3
"""P4 切割方案消歧：多链重复 → primary_chain 语义裁决 + related_chains
原则（B3 多链共享）：
- 每只股票 primary_chain=语义最贴切子链（按主营关键词命中分+宽链血缘优先）
- 其余出现作为 related_chains 保留（多链共振是强化信号）
- 军工双链合并: 军工-装备制造链 并回 军工产业链
- 父链修正: 有色-* → 有色金属产业链
输出: data/p4_prep/p4_cut_plan_disambig.json { 子链: {parent, stocks[], related_stocks[]} }
"""
import json
from collections import defaultdict

BASE = '/home/agentuser/data/定时任务打包/data'
d = json.load(open(f'{BASE}/p4_prep/p4_cut_plan_final.json'))
subs = d['subchains']
cross = d['cross_removed']
indep = d['independent']

# 1) 军工双链合并
if '军工-装备制造链' in subs and '军工产业链' in subs:
    subs['军工产业链']['stocks'].extend(subs.pop('军工-装备制造链')['stocks'])

# 2) 父链修正（前缀映射）
FAM = {'有色': '有色金属产业链', '化工': '化工产业链', '医药': '医药产业链',
       'AI应用': 'AI应用产业链', 'AI算力': 'AI算力产业链', '半导体材料': '半导体材料产业链',
       '光通信': '光通信产业链', '电子材料': '电子材料产业链', '金融科技': '金融科技产业链',
       '交运': '交运产业链', '农业': '农业产业链'}
for s, v in subs.items():
    if v['parent'] is None and '-' in s:
        fam = s.split('-')[0]
        if fam in FAM: v['parent'] = FAM[fam]

# 3) 收集每股所有子链 → primary 裁决
# 子链语义关键词（主营文本匹配，按 子链名-前缀 推断 + 已有子链内 yw 样例）
# 用"子链内已归股票的共同语义"做判定成本高；采用简化：优先保留原宽链（血缘），
# 跨链重复杂交时按命中分最高子链为主；同分按股票少者优先
code_chains = defaultdict(list)  # code -> [(sub, parent, idx, score)]
for sub, v in subs.items():
    for st in v['stocks']:
        code_chains[st['code']].append((sub, v.get('parent'), st))

# 计分：主营文本在该子链关键词表命中数（用 p4_cut_plan_final 的同款 KEYADD 扩展为正则近似）
# 简化：用子链名核心词 + 该股 yw 是否含子链前缀词
def score_sub(sub, yw, products):
    text = (yw or '') + ' ' + ' '.join(products or [])
    # 从子链名提取核心词（如 化工-煤化链 → 煤化；医药-CXO链 → CXO）
    core = sub.split('-')[-1].replace('链', '')
    # 通用子链词表（覆盖主要细分）
    KW = {
        '煤化': ['煤', '甲醇', '尿素', '烯烃', '醋酸', '华鲁', '宝丰'],
        '化纤': ['涤纶', '氨纶', '粘胶', '纤维', '聚酯', 'PTA', '长丝'],
        '农化': ['化肥', '磷', '钾', '农药', '农资', '复合肥'],
        '精细化工': ['树脂', '颜料', '助剂', '硅', '氟', '精细', '纳米', '水处理', '添加剂'],
        '工业气体': ['气体', '二氧化碳', '氮气'],
        '润滑油': ['润滑油'],
        '创新药': ['药', '制药', '原料药', '制剂', '医药', '创新药', '中成药', '药品'],
        '生物制品': ['疫苗', '血液', '生物制品', '血制品'],
        'CXO': ['CRO', 'CDMO', '研发服务', '临床', '模型', '试剂', '蛋白'],
        '器械诊断': ['诊断', '器械', 'IVD', '检测', '医疗电子'],
        '医疗服务': ['医院', '医疗健康', '康复', '医疗服务'],
        '工业金属': ['铜', '铝', '铅', '锌', '锡', '镍', '矿业', '冶炼', '采选'],
        '贵金属': ['黄金', '金矿', '白银', '银'],
        '能源金属': ['锂', '钴', '正极', '负极', '前驱体'],
        '稀土小金属': ['稀土', '钨', '钼', '锗', '锑', '靶材', '钛'],
        '黄金珠宝': ['珠宝', '首饰', '钻石', '黄金珠宝'],
        '影视内容': ['影视', '电影', '电视剧', '动漫', '院线', '剧集'],
        '出版传媒': ['出版', '图书', '期刊', '传媒', '广电', '有线', 'IPTV', '阅读'],
        '广告营销': ['广告', '营销', '公关', '品牌', '流量', '直播'],
        '游戏互娱': ['游戏', '手游', '互娱', '娱乐'],
        'AI技术': ['语音', 'AI', '算法', '智能', '讯飞', '训练数据'],
        'IDC云': ['IT', '服务器', '云计算', 'IDC', '数据中心', '云'],
        '算力芯片': ['处理器', 'CPU', 'GPU', '芯片', '存储'],
        '半导体设备': ['设备', '刻蚀', '洗净'],
        '材料': ['靶材', '光刻胶', '石英', '硅片', '材料', '封装材料', '电子特气'],
        '设计': ['芯片', '集成电路', 'SoC', 'MCU', '模拟', '电源', 'IC'],
        '封测': ['封装', '测试', '封测', 'PCB', '载板'],
        '光模块': ['光', '光纤', '光缆', '模块', '器件', '通信'],
        '激光光学': ['激光', '光学', '透镜', '红外'],
        '覆铜板': ['覆铜板', '铜箔', '玻纤', '电子布', '树脂', '环氧'],
        'PCB': ['PCB', '印制电路板', '电路板'],
        '电子化学品': ['电子化学品', '纳米', '金属粉体', '改性'],
        '金融IT': ['金融', '证券', '银行', '软件', '支付', '卡', '数据', 'IT', '解决方案'],
        '网络安全': ['安全', '密码', '认证', '防火墙'],
        '财税政务': ['政务', '财税', '税务', '金税', '信息化'],
        '港口': ['港口', '码头', '港务', '集装箱'],
        '航运': ['航运', '海运', '油运', '船舶', '运输'],
        '公路铁路': ['高速', '公路', '铁路', '高铁', '机场'],
        '装备制造': ['军工', '兵器', '导弹', '火箭', '卫星', '航天', '防务', '雷达', '导航', '军品', '弹药'],
        '种业': ['种', '水稻', '玉米', '小麦', '育种'],
        '养殖饲料': ['养殖', '生猪', '鸡', '饲料', '水产', '虾', '捕捞'],
        '农产品加工': ['粮油', '糖', '乳', '食品', '棉花', '大豆', '加工'],
    }
    core = sub.split('-')[-1].replace('链', '')
    keys = KW.get(core, [core])
    n = sum(1 for k in keys if k in text)
    return n

primary = {}   # code -> sub
related = defaultdict(list)  # code -> [sub...]
parent_map = {}
for code, items in code_chains.items():
    # 去重（同子链可能多行）
    dedup = {}
    for sub, par, st in items:
        dedup[sub] = (par, st)
    if len(dedup) == 1:
        sub = list(dedup.keys())[0]
        primary[code] = sub
        parent_map[code] = dedup[sub][0]
        continue
    # 多链：按主营命中分裁决 primary（同分 → 按子链名长度短的/字母序稳定）
    scored = []
    for sub, (par, st) in dedup.items():
        s = score_sub(sub, st.get('yw',''), st.get('products',[]))
        scored.append((s, sub, par, st))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = scored[0]
    primary[code] = best[1]
    parent_map[code] = best[2]
    for s, sub, par, st in scored[1:]:
        related[code].append(sub)

# 4) 重建子链表
out_subs = defaultdict(lambda: {'parent': None, 'stocks': [], 'related_stocks': []})
for code, sub in primary.items():
    # 找到该股在 sub 的行信息
    st = code_chains[code][0][2]  # 用第一个出现的信息（同 code 同名同 yw）
    out_subs[sub]['stocks'].append(st)
    out_subs[sub]['parent'] = parent_map[code]
for code, rels in related.items():
    for sub in rels:
        st = code_chains[code][0][2]
        out_subs[sub]['related_stocks'].append({'code': code, 'name': st.get('name','')})

result = {'subchains': dict(out_subs), 'cross_removed': cross, 'independent': indep,
          'meta': {'n_subchains': len(out_subs), 'n_primary': len(primary), 'n_related': len(related)}}
json.dump(result, open(f'{BASE}/p4_prep/p4_cut_plan_disambig.json','w'), ensure_ascii=False, indent=1)

for sub, v in sorted(out_subs.items()):
    rel = len(v['related_stocks'])
    print(f"{sub} (parent={v['parent']}): primary={len(v['stocks'])} related={rel}")
print(f"\n总计: 子链{len(out_subs)} primary{len(primary)} related{len(related)}")
json.dump({k: len(v) for k, v in {s: v['stocks'] for s, v in out_subs.items()}.items()}, open('/tmp/p4_sub_counts.json','w'))