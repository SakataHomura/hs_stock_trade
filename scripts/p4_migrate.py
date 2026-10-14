#!/usr/bin/env python3
"""P4 存量迁移执行脚本（幂等，可重跑）
依据: 数据/p4_prep/p4_cut_plan_disambig.json（50 子链）
动作:
  1. 11 条切割宽链 10-09 记录 is_legacy=1（银行/军工单链族保留原名不废弃）
  2. theme_constituents 初始化（52 链名 = 44 切割子链 + 银行/军工 + 6 池外新链）
  3. industry_themes 44 子链 INSERT（10-09，peak_count 新起算=primary数，status 沿用宽链）
  4. theme_fundamentals 宽链→子链复制 + 池外新链轻量初始化
  5. fade_watch 旧宽链非 dropped 记录标 dropped（P4 备注）
⚠️ 全部在事务外逐条执行；每条带 WHERE/ON DUPLICATE 保证幂等
"""
import json, sys
import pymysql

BASE = '/home/agentuser/data/定时任务打包/data'
PLAN = json.load(open(f'{BASE}/p4_prep/p4_cut_plan_disambig.json'))
SUBS = PLAN['subchains']

conn = pymysql.connect(host='127.0.0.1', user='wish', password='<DB_PASSWORD>', database='portfolio', charset='utf8mb4')
cur = conn.cursor()

MIGRATE_DATE = '2026-10-09'
ADD_DATE = '2026-10-10'

# 宽链当日状态/峰值（10-09）
cur.execute("SELECT theme_name,status,related_sectors,consecutive_days FROM industry_themes WHERE theme_date=%s", (MIGRATE_DATE,))
wide_status = {r[0]: {'status': r[1], 'related_sectors': r[2], 'cd': r[3]} for r in cur.fetchall()}

# 池外新链（parent=None 且非 13 宽链名的独立链）
PARENT_NONE_NEW = {'电池产业链', '黄金珠宝产业链', '化妆品产业链', '机械设备产业链', '油气服务产业链', '零售产业链'}

stats = {'legacy': 0, 'const_ins': 0, 'const_upd': 0, 'sub_ins': 0, 'fund_ins': 0, 'fade_drop': 0}

# ---- 1. 宽链废弃（11 条切割族；银行/军工保留） ----
cut_fams = set()
for sub, v in SUBS.items():
    p = v['parent']
    if p and p not in cut_fams and p not in ('银行产业链', '军工产业链'):
        cut_fams.add(p)
for fam in sorted(cut_fams):
    cur.execute("UPDATE industry_themes SET is_legacy=1 WHERE theme_date=%s AND theme_name=%s", (MIGRATE_DATE, fam))
    stats['legacy'] += cur.rowcount
print(f"[1] 宽链废弃标记 is_legacy=1: {stats['legacy']} 条 ({sorted(cut_fams)})")

# ---- 2. theme_constituents 初始化 ----
for sub, v in SUBS.items():
    for st in v['stocks']:
        ev = (st.get('yw') or '')[:120] or None
        rel = v.get('related_stocks', [])
        rel_chains = None
        # related 只记录该股在其他子链的 related（由 disambig 的 related 字段给出，这里简化为备注）
        cur.execute("""INSERT INTO theme_constituents
            (theme_name, stock_code, stock_name, primary_chain, related_chains, biz_evidence, biz_report_date, add_date, is_current)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)
            ON DUPLICATE KEY UPDATE stock_name=VALUES(stock_name), biz_evidence=VALUES(biz_evidence),
                biz_report_date=VALUES(biz_report_date), is_current=1, remove_date=NULL""",
            (sub, st['code'], st.get('name') or '', sub,
             json.dumps([r['code'] for r in rel], ensure_ascii=False) if rel else None,
             ev, st.get('report_date'), ADD_DATE))
        stats['const_ins'] += 1
        stats['const_upd'] += cur.rowcount == 2
print(f"[2] theme_constituents 初始化: {stats['const_ins']} 行")

# ---- 3. industry_themes 子链 INSERT（44 切割子链；银行/军工单链族保留原名原记录不重建；池外新链不 INSERT 等 16:00 确认） ----
# ⚠️ industry_themes 无 (theme_name,theme_date) 唯一键，ON DUPLICATE 失效 → 幂等靠先删后插
# ⚠️ 单链族保护：parent==sub（银行/军工产业链）不 DELETE 重插，保留原始记录原 id/字段，仅确保 is_legacy=0
for sub, v in SUBS.items():
    parent = v['parent']
    if sub in PARENT_NONE_NEW:
        continue  # 池外新链：成分基线预填，主线记录等 16:00 确认流程
    if parent == sub:
        # 单链族（银行/军工）：保留原有 10-09 记录（id/peak_count/peak_date/score/advice 均以基线为准不覆盖）
        cur.execute("UPDATE industry_themes SET is_legacy=0 WHERE theme_date=%s AND theme_name=%s",
                    (MIGRATE_DATE, sub))
        stats['sub_ins'] += 0
        print(f"[3][单链族保护] {sub}: 保留原始记录，仅确认 is_legacy=0")
        continue
    # 幂等：先删当日子链（保留宽链 is_legacy=1 不动）
    cur.execute("DELETE FROM industry_themes WHERE theme_date=%s AND theme_name=%s AND is_legacy=0",
                (MIGRATE_DATE, sub))
    w = wide_status.get(parent, {})
    status = w.get('status', 'active')
    n = len(v['stocks'])
    # 子链 sectors：沿用宽链 related_sectors（保障 fetch_trend sectors 回退），concepts 同
    rs = w.get('related_sectors') or '{}'
    cur.execute("""INSERT INTO industry_themes
        (theme_name, theme_date, status, consecutive_days, related_sectors, peak_count, peak_date, parent_theme, signal_peak, break_level, advice_signal, score_heat, score_tech, score_fund, is_legacy)
        VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,0,NULL,3,2,2,0)""",
        (sub, MIGRATE_DATE, status, rs, n, MIGRATE_DATE, parent, n))
    stats['sub_ins'] += 1
print(f"[3] industry_themes 子链 INSERT: {stats['sub_ins']} 条")

# ---- 4. theme_fundamentals 迁移 ----
cur.execute("SELECT theme_name,fund_score,logic_type,logic_desc,eval_date FROM theme_fundamentals")
fund_old = {r[0]: r for r in cur.fetchall()}
for sub, v in SUBS.items():
    parent = v['parent']
    if sub in PARENT_NONE_NEW:
        # 池外新链轻量初始化
        cur.execute("""INSERT IGNORE INTO theme_fundamentals (theme_name,fund_score,logic_type,logic_desc,eval_date)
            VALUES (%s,2,'policy_cycle','池外新链(P4承接)，主营归因成立',%s)""", (sub, MIGRATE_DATE))
        stats['fund_ins'] += 1
        continue
    src = fund_old.get(parent)
    if src:
        cur.execute("""INSERT INTO theme_fundamentals (theme_name,fund_score,logic_type,logic_desc,eval_date)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE fund_score=VALUES(fund_score), logic_type=VALUES(logic_type),
                logic_desc=VALUES(logic_desc), eval_date=VALUES(eval_date)""",
            (sub, src[1], src[2], src[3], src[4]))
        stats['fund_ins'] += 1
    else:
        cur.execute("""INSERT IGNORE INTO theme_fundamentals (theme_name,fund_score,logic_type,logic_desc,eval_date)
            VALUES (%s,2,'policy_cycle','P4子链初始化',%s)""", (sub, MIGRATE_DATE))
        stats['fund_ins'] += 1
print(f"[4] theme_fundamentals: {stats['fund_ins']} 条")

# ---- 5. fade_watch 旧宽链非 dropped 标 dropped（P4 迁移备注） ----
for fam in sorted(cut_fams):
    cur.execute("""UPDATE faded_theme_watch SET watch_status='dropped', last_signal_type='p4_migrated',
        last_check_date=%s WHERE theme_name=%s AND watch_status<>'dropped'""", (ADD_DATE, fam))
    stats['fade_drop'] += cur.rowcount
print(f"[5] fade_watch 旧宽链清理: {stats['fade_drop']} 条")

conn.commit()
print("\n✅ P4 迁移执行完成")
print(json.dumps(stats, ensure_ascii=False))