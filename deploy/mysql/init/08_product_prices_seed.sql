-- =============================================================
-- 商品价格周期种子（2026-11）
--
-- 每个 SKU 生成三个周期价目：历史价 / 当期价 / 未来价（未生效）。
-- 当期价与 products.price 保持一致，避免前端展示与基准价不一致。
-- 幂等：uk_sku_price_version(sku, price_version) 冲突时更新价格与周期。
-- =============================================================

-- 1) 历史价（已结束）
INSERT INTO product_prices (sku, price, effective_from, effective_to, price_version, note)
SELECT sku, ROUND(price * 0.92, 2), '2026-01-01', '2026-10-31', '2026-Q3', '历史价目，仅供 10 月前订单核对'
FROM products WHERE deleted = 0 AND status = 1
ON DUPLICATE KEY UPDATE price=VALUES(price), effective_from=VALUES(effective_from),
  effective_to=VALUES(effective_to), note=VALUES(note);

-- 2) 当期价（2026-11 生效，与 products.price 一致）
INSERT INTO product_prices (sku, price, effective_from, effective_to, price_version, note)
SELECT sku, price, '2026-11-01', '2026-11-30', '2026-11', '11 月价目（现行）'
FROM products WHERE deleted = 0 AND status = 1
ON DUPLICATE KEY UPDATE price=VALUES(price), effective_from=VALUES(effective_from),
  effective_to=VALUES(effective_to), note=VALUES(note);

-- 3) 未来价（12 月起生效，当前不得提前返回）
INSERT INTO product_prices (sku, price, effective_from, effective_to, price_version, note)
SELECT sku, ROUND(price * 1.06, 2), '2026-12-01', '2026-12-31', '2026-12', '12 月拟调价目（未生效）'
FROM products WHERE deleted = 0 AND status = 1
ON DUPLICATE KEY UPDATE price=VALUES(price), effective_from=VALUES(effective_from),
  effective_to=VALUES(effective_to), note=VALUES(note);
