-- =============================================================
-- 2026-11-14 · orders 增加收件人姓名/电话
--
-- 背景：写操作面收窄为「改收货地址 / 改联系电话」。原有 orders 只有
-- shipping_address 字符串，没有电话字段，改电话无处落库。
--
-- 幂等设计：MySQL 8 的 ADD COLUMN 不支持 IF NOT EXISTS，这里用
-- information_schema 预检 + PREPARE 动态语句，重复执行安全（容器初始化
-- 脚本可能被多次跑，失败会中断整个 init）。
-- =============================================================

SET @has_name := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'receiver_name'
);
SET @sql := IF(@has_name = 0,
  'ALTER TABLE `orders` ADD COLUMN `receiver_name` VARCHAR(64) DEFAULT NULL COMMENT ''收件人姓名（改电话写操作引入）'' AFTER `address_id`',
  'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_phone := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders' AND COLUMN_NAME = 'receiver_phone'
);
SET @sql := IF(@has_phone = 0,
  'ALTER TABLE `orders` ADD COLUMN `receiver_phone` VARCHAR(32) DEFAULT NULL COMMENT ''收件人电话（改电话写操作引入）'' AFTER `receiver_name`',
  'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
