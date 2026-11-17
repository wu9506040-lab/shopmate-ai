"""抖店渠道 —— 通过抖音电商开放平台查买家在本店的订单。

接口：order.searchList（按门店 + 买家 open_id 检索订单）
网关：https://openapi-fxg.jinritemai.com

签名（抖店 md5 方式）：
    1. 公共参数（app_key/method/param_json/timestamp/v/sign_method）按 key 字典序排序
    2. 拼成 key1value1key2value2...（param_json 是 JSON 字符串整体参与）
    3. sign = MD5(app_secret + 拼接串 + app_secret).lower()

金额单位是「分」，务必除以 100 —— 抖店这里按分返，不转换会答成"您支付了 19900 元"。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Optional

from app.core.config import settings
from app.schemas.business import Order, OrderItem
from app.services.order.channels._platform import (
    PlatformCallError,
    post_form,
    to_dial_status,
)
from app.services.order.channels.base import BuyerRef, ChannelQueryResult, ChannelStatus

logger = logging.getLogger(__name__)

GATEWAY = "https://openapi-fxg.jinritemai.com"


def sign_doudian(params: dict[str, Any], secret: str) -> str:
    """抖店 md5 签名（纯函数，可单测）。"""
    pieces = [
        f"{k}{params[k]}"
        for k in sorted(params)
        if k != "sign" and params[k] not in (None, "")
    ]
    raw = secret + "".join(pieces) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().lower()


def _unix_to_dt(value: Any) -> datetime:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return datetime.now()
    # 抖店偶尔返毫秒
    if ts > 10_000_000_000:
        ts //= 1000
    return datetime.fromtimestamp(ts)


class DouyinChannel:
    """抖店订单渠道"""

    name = "douyin"

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        shop_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._app_key = app_key if app_key is not None else getattr(settings, "DOUYIN_APP_KEY", "")
        self._app_secret = (
            app_secret if app_secret is not None else getattr(settings, "DOUYIN_APP_SECRET", "")
        )
        self._shop_id = shop_id if shop_id is not None else getattr(settings, "DOUYIN_SHOP_ID", "")
        self._timeout = timeout if timeout is not None else float(
            getattr(settings, "ORDER_CHANNEL_TIMEOUT", 2.0)
        )

    def is_configured(self) -> bool:
        return bool(self._app_key and self._app_secret and self._shop_id)

    async def _call(self, method: str, biz: dict[str, Any]) -> dict:
        params: dict[str, Any] = {
            "app_key": self._app_key,
            "method": method,
            "param_json": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "v": "2",
            "sign_method": "md5",
        }
        params["sign"] = sign_doudian(params, self._app_secret)
        body = await post_form(f"{GATEWAY}/{method}", params, timeout=self._timeout)
        # 抖店：code=0/err_no=0 才是成功，否则 data 是空的
        code = body.get("code", body.get("err_no"))
        if code not in (0, "0", None):
            raise PlatformCallError(
                f"抖店错误 {code}: {body.get('msg') or body.get('message')}", code=str(code),
            )
        return body

    async def list_user_orders(
        self, buyer: BuyerRef, status: Optional[str] = None, limit: int = 20,
    ) -> ChannelQueryResult:
        if not self.is_configured():
            return ChannelQueryResult(self.name, ChannelStatus.DISABLED, [],
                                      "未配置 DOUYIN_APP_KEY/SECRET/SHOP_ID")
        buyer_id = buyer.platform_id(self.name)
        if not buyer_id:
            return ChannelQueryResult(self.name, ChannelStatus.UNBOUND, [], "该用户未绑定抖音账号")

        biz: dict[str, Any] = {
            "shop_id": self._shop_id,
            "open_id": buyer_id,
            "page": 0,
            "size": min(limit, 50),
            "order_by": "create_time",
            "order": "desc",
        }
        if status:
            biz["order_status"] = status
        try:
            body = await self._call("order.searchList", biz)
        except PlatformCallError as e:
            st = ChannelStatus.TIMEOUT if e.timeout else ChannelStatus.ERROR
            return ChannelQueryResult(self.name, st, [], str(e))

        data = body.get("data") or {}
        rows = data.get("shop_order_list") or data.get("order_list") or []
        if isinstance(rows, dict):
            rows = [rows]
        return ChannelQueryResult(
            self.name, ChannelStatus.OK, [self._to_order(r) for r in rows],
        )

    async def get_order(self, buyer: BuyerRef, order_no: str) -> Optional[Order]:
        if not self.is_configured():
            return None
        biz = {"shop_id": self._shop_id, "order_id": order_no}
        try:
            body = await self._call("order.orderDetail", biz)
        except PlatformCallError as e:
            logger.info(f"douyin get_order failed: order_id={order_no}, {e}")
            return None
        data = body.get("data") or {}
        row = data.get("shop_order_detail") or data.get("order_detail") or data
        if not row or not (row.get("order_id") or row.get("order_no")):
            return None
        # 防越权：核对买家
        buyer_id = buyer.platform_id(self.name)
        actual = row.get("open_id") or (row.get("user") or {}).get("open_id")
        if buyer_id and actual and str(actual) != str(buyer_id):
            logger.warning(f"douyin 越权拦截: order_id={order_no} 买家不匹配")
            return None
        return self._to_order(row)

    # ---------- DTO 映射 ----------
    def _to_order(self, r: dict) -> Order:
        raw_skus = r.get("sku_order_list") or r.get("product_list") or []
        if isinstance(raw_skus, dict):
            raw_skus = [raw_skus]
        items = []
        for s in raw_skus:
            qty = int(s.get("item_num") or s.get("combo_num") or 1)
            # 抖店金额单位是分
            unit = float(s.get("goods_price") or s.get("item_price") or 0) / 100.0
            items.append(
                OrderItem(
                    sku=str(s.get("outer_sku_id") or s.get("sku_id") or s.get("product_id") or ""),
                    product_name=str(s.get("product_name") or s.get("goods_name") or ""),
                    quantity=qty,
                    unit_price=unit,
                    subtotal=unit * qty,
                )
            )
        pay = r.get("pay_amount")
        total = float(pay) / 100.0 if pay is not None else sum(i.subtotal for i in items)
        receiver = r.get("post_receiver") or {}
        return Order(
            order_no=str(r.get("order_id") or r.get("order_no") or ""),
            user_id=0,
            status=to_dial_status(str(r.get("order_status") or "")),
            items=items,
            total_amount=total,
            receiver_name=receiver.get("name") if isinstance(receiver, dict) else None,
            receiver_phone=receiver.get("phone") if isinstance(receiver, dict) else None,
            create_time=_unix_to_dt(r.get("create_time")),
            update_time=_unix_to_dt(r.get("update_time") or r.get("create_time")),
            channel=self.name,
        )
