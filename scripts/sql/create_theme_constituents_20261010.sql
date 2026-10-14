-- ============================================================
-- 主线聚合下钻细化 P2-1 数据模型变更（设计文档 20261010 §8）
-- 执行日期：2026-10-10 ｜ 执行人：主 agent（独立于 codex）
-- ============================================================

USE portfolio;

-- 1) industry_themes 加链族/次属/信号峰值字段（§8）
ALTER TABLE industry_themes
  ADD COLUMN parent_theme VARCHAR(100) DEFAULT NULL COMMENT '链族名（19:30 归约展示层，D4）',
  ADD COLUMN related_chains TEXT DEFAULT NULL COMMENT '次属链 JSON [\"链名\",...]（B1 披露+半票）',
  ADD COLUMN signal_peak INT DEFAULT NULL COMMENT '当日信号口径峰值（与历史 peak_count 分离，C5 晋升/监控用）',
  ADD KEY idx_parent_theme (parent_theme);

-- 2) 新表 theme_constituents 成分基线（§5.1 C1）
CREATE TABLE IF NOT EXISTS theme_constituents (
  id INT NOT NULL AUTO_INCREMENT,
  theme_name VARCHAR(100) NOT NULL COMMENT '子链名（= 本行主链名）',
  stock_code VARCHAR(20) NOT NULL COMMENT '股票代码 sh/sz 前缀',
  stock_name VARCHAR(50) DEFAULT NULL,
  primary_chain VARCHAR(100) NOT NULL COMMENT '主属链（=theme_name）',
  related_chains TEXT DEFAULT NULL COMMENT '次属链 JSON',
  biz_evidence TEXT DEFAULT NULL COMMENT '主营证据（引用主营原文/归因依据）',
  biz_report_date VARCHAR(20) DEFAULT NULL COMMENT '主营报告期（D2 变更检测跨期 diff）',
  add_date DATE NOT NULL COMMENT '入基线日期',
  remove_date DATE DEFAULT NULL COMMENT '移出日期（NULL=当前有效）',
  is_current TINYINT NOT NULL DEFAULT 1 COMMENT '1=当前有效成分 0=历史（已移除）',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_theme_stock (theme_name, stock_code),
  KEY idx_primary (primary_chain),
  KEY idx_current (is_current),
  KEY idx_stock (stock_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='主线成分基线（C1，指数合成源）';

-- 3) 新表 theme_constituent_changes 变更日志（§5.3 C3）
CREATE TABLE IF NOT EXISTS theme_constituent_changes (
  id INT NOT NULL AUTO_INCREMENT,
  theme_name VARCHAR(100) NOT NULL COMMENT '子链名',
  stock_code VARCHAR(20) NOT NULL,
  change_type VARCHAR(20) NOT NULL COMMENT 'add/remove/promote/demote',
  reason VARCHAR(50) NOT NULL COMMENT '主营变更/新上市/退市/晋升/休眠',
  evidence TEXT DEFAULT NULL COMMENT '证据（主营 diff/上榜记录）',
  change_date DATE NOT NULL,
  rollback_snapshot TEXT DEFAULT NULL COMMENT '按日快照回滚（变更前完整成分 JSON）',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_theme_date (theme_name, change_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='成分变更日志 + 回滚快照（C3）';

-- 4) faded_theme_watch.fade_reason 扩枚举（§8：identity_drift / tech_fade / cap_exceed）
--    列本身 VARCHAR(200) 无需 DDL，枚举为语义约束，写库侧 codex 实现时对齐即可。
--    此处加注释说明扩展值，供审计参照。
ALTER TABLE faded_theme_watch
  MODIFY COLUMN fade_reason VARCHAR(200) DEFAULT NULL COMMENT '退潮原因：状态机fading/ended / 数量上限挤出 / 评分降级观察线 / identity_drift(链身份漂移) / tech_fade(技术退潮) / cap_exceed(数量上限)';

-- 5) theme_fundamentals PK：设计 §8 要求 PK 改子链名——当前列已是 theme_name PK，
--    子链粒度由 P4 存量迁移时写入子链名实现（PK 结构无需变更），此处不加 DDL。

-- 验证
SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema='portfolio' AND table_name IN ('theme_constituents','theme_constituent_changes');
SHOW COLUMNS FROM industry_themes;