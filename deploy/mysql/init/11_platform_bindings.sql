-- =============================================================
-- platform_buyer_bindings 平台买家身份绑定
--
-- 作用：把本地 users.id 映射到各电商平台的买家 ID，订单查询才能去
--       淘宝 / 抖店 等平台接口按人查单。
--
-- 为什么要单独一张表：
--   平台侧的 buyer 标识（淘宝 open_uid / 抖店 open_id）和本地 user_id
--   没有任何关系，必须显式绑定。没有绑定就不能查——拿本地 user_id 去平台
--   接口查回来的单可能不是同一个人的，属于越权。
--
-- 一个用户在一个平台只有一条有效绑定（uk_user_platform 保证）。
-- =============================================================

USE `customer_service`;

CREATE TABLE IF NOT EXISTS `platform_buyer_bindings` (
  `id`                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT        COMMENT '主键',
  `user_id`            BIGINT UNSIGNED NOT NULL                       COMMENT '本地用户（逻辑外键 → users.id）',
  `platform`           VARCHAR(32)  NOT NULL                          COMMENT '平台标识：taobao / douyin / pdd',
  `platform_buyer_id`  VARCHAR(128) NOT NULL                          COMMENT '平台侧买家 ID（淘宝 open_uid / 抖店 open_id）',
  `shop_id`            VARCHAR(64)  DEFAULT NULL                      COMMENT '平台店铺 ID（抖店必填，淘宝可空）',
  `create_time`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_platform` (`user_id`, `platform`),
  KEY `idx_platform_buyer` (`platform`, `platform_buyer_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='平台买家身份绑定（订单多渠道查询用）';
