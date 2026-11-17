-- =============================================================
-- user_long_term_facts 跨会话长期事实（L3）
-- 白名单槽位，一个用户一个 key 一行；可单条查看/删除/过期。
-- 删除用 deleted=1 作 tombstone，禁止被后续写入悄悄复活。
-- =============================================================

USE `customer_service`;

CREATE TABLE IF NOT EXISTS `user_long_term_facts` (
  `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT        COMMENT '主键',
  `user_id`           BIGINT UNSIGNED NOT NULL                       COMMENT '所属用户（逻辑外键 → users.id）',
  `fact_key`          VARCHAR(32)  NOT NULL                          COMMENT '白名单 key：size / fabric_pref / after_sales_conclusion',
  `fact_value`        VARCHAR(128) NOT NULL                          COMMENT '事实值（标量，不含身份信息）',
  `prev_value`        VARCHAR(128) DEFAULT NULL                      COMMENT '改口前的值，仅供追溯',
  `source_session_id` VARCHAR(64)  NOT NULL                          COMMENT '首次确认该值的会话',
  `hits`              INT          NOT NULL DEFAULT 1                COMMENT '跨会话出现次数（同一会话重复不累加）',
  `active`            TINYINT      NOT NULL DEFAULT 0                COMMENT '1=跨会话已确认，可注入 prompt；0=候选',
  `expire_at`         DATETIME     NOT NULL                          COMMENT '过期时间（写入期给出，读时过滤）',
  `create_time`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted`           TINYINT      NOT NULL DEFAULT 0                COMMENT 'tombstone：删除后置 1，永不复活',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_l3_user_key` (`user_id`, `fact_key`),
  KEY `idx_l3_expire` (`expire_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='跨会话长期事实（L3，白名单+可删除）';
