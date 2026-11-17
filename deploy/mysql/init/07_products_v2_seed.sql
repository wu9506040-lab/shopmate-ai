-- =============================================================
-- 商品目录状态校对（猫与小鼠 2026AW）
-- 与 04_products_seed.sql 同源：确保尺码 SKU 在售、库存一致
-- 幂等：只更新已存在的服饰 SKU，不新增、不清空
-- =============================================================
SET NAMES utf8mb4;
USE `customer_service`;

UPDATE `products`
SET `status` = 1, `deleted` = 0, `stock` = 100
WHERE `sku` LIKE 'FS%';
