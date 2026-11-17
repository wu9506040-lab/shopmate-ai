-- =============================================================
-- 2026-11-14 · 知识库文档版本化 + 商品价格周期化
--
-- 背景：同一政策/商品会有多份不同时间的版本；商品价格按周期调整。
-- 本脚本幂等：MySQL 8 的 ADD COLUMN / CREATE TABLE 不支持 IF NOT EXISTS，
-- 用 information_schema 预检 + PREPARE 动态语句，容器初始化脚本可重复执行。
-- =============================================================

-- ---------- 1. knowledge_documents 增加版本字段 ----------
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='knowledge_documents' AND COLUMN_NAME='doc_key');
SET @sql := IF(@c = 0,
  'ALTER TABLE `knowledge_documents` ADD COLUMN `doc_key` VARCHAR(64) DEFAULT NULL COMMENT ''主题键'' AFTER `doc_type`, ADD COLUMN `revision` VARCHAR(32) DEFAULT NULL COMMENT ''版本号'' AFTER `doc_key`, ADD COLUMN `validity` VARCHAR(16) NOT NULL DEFAULT ''permanent'' COMMENT ''permanent/temporary'' AFTER `revision`, ADD COLUMN `effective_from` BIGINT NOT NULL DEFAULT 0 COMMENT ''生效时间戳'' AFTER `validity`, ADD COLUMN `effective_to` BIGINT NOT NULL DEFAULT 0 COMMENT ''失效时间戳'' AFTER `effective_from`, ADD INDEX `idx_knowledge_doc_key_time` (`doc_key`, `effective_from`)',
  'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------- 2. product_prices 商品价格历史表 ----------
SET @t := (SELECT COUNT(*) FROM information_schema.TABLES
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='product_prices');
SET @sql := IF(@t = 0,
  'CREATE TABLE `product_prices` ( `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, `sku` VARCHAR(50) NOT NULL COMMENT ''SKU'', `price` DECIMAL(10,2) NOT NULL COMMENT ''该周期售价'', `effective_from` DATE NOT NULL COMMENT ''生效日（含）'', `effective_to` DATE DEFAULT NULL COMMENT ''失效日（含）；NULL=长期'', `price_version` VARCHAR(32) NOT NULL DEFAULT '''' COMMENT ''价目版本（如 2026-11）'', `note` VARCHAR(200) DEFAULT NULL COMMENT ''调价说明'', `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (`id`), UNIQUE KEY `uk_sku_price_version` (`sku`, `price_version`), KEY `idx_sku_effective` (`sku`, `effective_from`) ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT=''商品价格历史（按周期生效）''',
  'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
