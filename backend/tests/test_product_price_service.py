"""商品当期价目 集成测试（真实 MySQL，验证按日期自动切换）

为什么用真库而不是 mock：这一层的核心就是 SQL 的日期区间与排序，
mock 掉数据库等于把要验证的逻辑也 mock 走了。
"""
from __future__ import annotations

from app.services.product_price_service import get_current_price, get_current_prices

SKU = "FS368998-M"


def test_current_price_is_november_period():
    p = get_current_price(SKU, today="2026-11-14")
    assert p is not None
    assert p.version == "2026-11"
    assert p.effective_from == "2026-11-01"
    assert p.effective_to == "2026-11-30"
    assert p.price > 0
    assert "2026-11" in p.label


def test_historical_period_used_for_october():
    p = get_current_price(SKU, today="2026-10-15")
    assert p is not None and p.version == "2026-Q3"


def test_future_period_used_for_december():
    p = get_current_price(SKU, today="2026-12-05")
    assert p is not None and p.version == "2026-12"


def test_price_changes_between_periods():
    """用户的核心诉求：商品价格每个周期会变，不能一直是同一个数字"""
    prices = [
        get_current_price(SKU, today=d).price
        for d in ("2026-10-15", "2026-11-14", "2026-12-05")
    ]
    assert len(set(prices)) == 3, prices


def test_no_period_returns_none_for_unknown_sku():
    assert get_current_price("NOT-EXIST-SKU", today="2026-11-14") is None


def test_batch_query_does_not_drop_skus():
    # 用当前在售的真实 SKU：HKZ/MY 前缀的货没上架（products 表 0 行），
    # 拿它们断言"不丢 SKU"等于测一个不存在的商品，永远红
    skus = ["FS368998-M", "FS368998-L", "FS368998-XL"]
    prices = get_current_prices(skus, today="2026-11-14")
    assert all(s in prices for s in skus)
    assert all(p.version == "2026-11" for p in prices.values())
