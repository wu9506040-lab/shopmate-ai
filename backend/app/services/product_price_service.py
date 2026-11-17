"""商品价格周期服务（2026-11）

## 为什么单独一张历史价表

商品价格按周期调整，不可能一直不变。把价格写死在 `products.price` 会有两个问题：
1. 改价后历史订单无法核对"下单当时是多少钱"
2. AI 回答"多少钱"时无法给出依据与有效期，容易把旧价当现价

设计：
- `products.price` 保留为**基准价 / 兜底价**（没有生效价目时用）
- `product_prices` 存各周期价目：sku + 生效日/失效日 + 版本号 + 调价说明
- 展示与回答一律取「当前生效价目」；同一天有多条时取生效日最新、再取 id 最大

## 边界

- 未生效的价目（未来调价）绝不提前返回（与知识库版本选择同一套时间原则）
- 价目表查询失败 / 无记录 → 回落基准价，且 `price_version=None` 让上层知道
  "这是兜底价而非当期价"，避免把兜底价伪装成已核实的当期价
- 一次批量查询（IN），不在商品列表里做 N+1
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import bindparam, text

logger = logging.getLogger(__name__)

_TZ = _dt.timezone(_dt.timedelta(hours=8))


@dataclass(frozen=True)
class PricePeriod:
    sku: str
    price: float
    effective_from: str
    effective_to: Optional[str]
    version: str
    note: str = ""

    @property
    def label(self) -> str:
        """给 AI / 前端的一行人话"""
        if self.effective_to:
            return f"当期价（{self.effective_from} ~ {self.effective_to}，{self.version}）"
        return f"当期价（自 {self.effective_from} 起，{self.version}）"


def today_str() -> str:
    return _dt.datetime.now(_TZ).date().isoformat()


def get_current_prices(skus: list[str], today: Optional[str] = None) -> dict[str, PricePeriod]:
    """批量取当期生效价目（一次 SQL）。

    Args:
        skus: SKU 列表（空 → 直接返回 {}）
        today: 判定日期 YYYY-MM-DD（测试可注入，模拟 12 月价格切换）

    Returns:
        {sku: PricePeriod}；失败或无记录 → 不出现该 sku（上层回落基准价）
    """
    skus = [str(s) for s in (skus or []) if s]
    if not skus:
        return {}
    day = today or today_str()
    try:
        from app.clients.mysql_client import get_session_local

        stmt = text(
            "SELECT sku, price, effective_from, effective_to, price_version, note "
            "FROM product_prices "
            "WHERE sku IN :skus AND effective_from <= :day "
            "AND (effective_to IS NULL OR effective_to >= :day) "
            "ORDER BY sku, effective_from DESC, id DESC"
        ).bindparams(bindparam("skus", expanding=True))

        db = get_session_local()()
        try:
            rows = db.execute(stmt, {"skus": skus, "day": day}).mappings().all()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"当期价目查询失败（回落基准价）: {type(e).__name__}: {e}")
        return {}

    out: dict[str, PricePeriod] = {}
    for r in rows:
        sku = str(r["sku"])
        if sku in out:  # ORDER BY 已按生效日/id 降序，第一条就是最新
            continue
        ef = r["effective_from"]
        et = r["effective_to"]
        out[sku] = PricePeriod(
            sku=sku,
            price=float(r["price"]),
            effective_from=ef.isoformat() if hasattr(ef, "isoformat") else str(ef),
            effective_to=(et.isoformat() if hasattr(et, "isoformat") else str(et)) if et else None,
            version=str(r["price_version"] or ""),
            note=str(r["note"] or ""),
        )
    return out


def get_current_price(sku: str, today: Optional[str] = None) -> Optional[PricePeriod]:
    return get_current_prices([sku], today).get(sku)


__all__ = ["PricePeriod", "get_current_prices", "get_current_price", "today_str"]
