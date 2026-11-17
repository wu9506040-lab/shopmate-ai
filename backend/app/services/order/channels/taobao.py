"""淘宝渠道 —— 通过淘宝开放平台（TOP）查买家在本店的订单。

接口：taobao.trades.sold.get（卖家查"我卖出的"订单，可按买家 open_uid 过滤）
网关：https://eco.taobao.com/router/rest

签名（TOP 标准 md5 方式）：
    1. 公共参数 + 业务参数合并，去掉 sign 与空值
    2. 按 key 的 ASCII 升序排列
    3. 拼成 key1value1key2value2...
    4. sign = MD5(secret + 拼接串 + secret).upper()

凭证三件套缺一不可，缺任一 → is_configured()=False，整条渠道不发请求。
买家身份用 `platform_buyer_id`（淘宝 open_uid），不是本地 user_id。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
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

GATEWAY = "https://eco.taobao.com/router/rest"
# 只取客服/退款判断真正会用的字段，避免拉全量导致超限
FIELDS = (
    "tid,status,payment,created,modified,receiver_name,receiver_mobile,"
    "orders.title,orders.num,orders.price,orders.num_iid,orders.outer_sku_id"
)


def sign_top(params: dict[str, Any], secret: str) -> str:
    """TOP md5 签名（纯函数，可单测）。"""
    pieces = []
    for key in sorted(k for k in params if k != "sign" and params[k] not in (None, "")):
        pieces.append(f"{key}{params[key]}")
    raw = secret + "".join(pieces) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _parse_time(value: str | None) -> datetime:
    """淘宝时间格式：'2026-11-10 12:33:44'（无时区，按 UTC+8 的 naive 处理）"""
    if not value:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.now()


class TaobaoChannel:
    """淘宝订单渠道"""

    name = "taobao"

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        session_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._app_key = app_key if app_key is not None else getattr(settings, "TAOBAO_APP_KEY", "")
        self._app_secret = (
            app_secret if app_secret is not None else getattr(settings, "TAOBAO_APP_SECRET", "")
        )
        self._session = (
            session_key if session_key is not None
            else getattr(settings, "TAOBAO_SESSION_KEY", "")
        )
        self._timeout = timeout if timeout is not None else float(
            getattr(settings, "ORDER_CHANNEL_TIMEOUT", 2.0)
        )

    def is_configured(self) -> bool:
        return bool(self._app_key and self._app_secret and self._session)

    # ---------- 公共参数 ----------
    def _public_params(self, method: str) -> dict[str, Any]:
        return {
            "method": method,
            "app_key": self._app_key,
            "session": self._session,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "format": "json",
            "v": "2.0",
            "sign_method": "md5",
        }

    async def _call(self, method: str, biz: dict[str, Any]) -> dict:
        params = {**self._public_params(method), **biz}
        params["sign"] = sign_top(params, self._app_secret)
        body = await post_form(GATEWAY, params, timeout=self._timeout)
        if "error_response" in body:
            err = body["error_response"] or {}
            raise PlatformCallError(
                f"TOP 错误 {err.get('code')}: {err.get('sub_msg') or err.get('msg')}",
                code=str(err.get("code")),
            )
        return body

    async def list_user_orders(
        self, buyer: BuyerRef, status: Optional[str] = None, limit: int = 20,
    ) -> ChannelQueryResult:
        if not self.is_configured():
            return ChannelQueryResult(self.name, ChannelStatus.DISABLED, [],
                                      "未配置 TAOBAO_APP_KEY/SECRET/SESSION_KEY")
        buyer_id = buyer.platform_id(self.name)
        if not buyer_id:
            return ChannelQueryResult(self.name, ChannelStatus.UNBOUND, [], "该用户未绑定淘宝账号")

        biz: dict[str, Any] = {
            "fields": FIELDS,
            "buyer_open_id": buyer_id,
            "page_no": 1,
            "page_size": min(limit, 20),
            "use_has_next": "true",
        }
        if status:
            biz["status"] = status
        try:
            body = await self._call("taobao.trades.sold.get", biz)
        except PlatformCallError as e:
            st = ChannelStatus.TIMEOUT if e.timeout else ChannelStatus.ERROR
            return ChannelQueryResult(self.name, st, [], str(e))

        resp = (body.get("trades_sold_get_response") or {})
        trades = ((resp.get("trades") or {}).get("trade")) or []
        if isinstance(trades, dict):  # 单条时淘宝返 dict 不返 list
            trades = [trades]
        return ChannelQueryResult(
            self.name, ChannelStatus.OK, [self._to_order(t) for t in trades],
        )

    async def get_order(self, buyer: BuyerRef, order_no: str) -> Optional[Order]:
        if not self.is_configured():
            return None
        biz = {"fields": FIELDS, "tid": order_no}
        try:
            body = await self._call("taobao.trade.fullinfo.get", biz)
        except PlatformCallError as e:
            logger.info(f"taobao get_order failed: tid={order_no}, {e}")
            return None
        trade = ((body.get("trade_fullinfo_get_response") or {}).get("trade")) or None
        if not trade:
            return None
        # 防越权：卖家接口能看到全店订单，必须自己核对买家
        buyer_id = buyer.platform_id(self.name)
        actual = trade.get("buyer_open_id") or trade.get("buyer_open_uid")
        if buyer_id and actual and str(actual) != str(buyer_id):
            logger.warning(f"taobao 越权拦截: tid={order_no} 买家不匹配")
            return None
        return self._to_order(trade)

    # ---------- DTO 映射 ----------
    def _to_order(self, t: dict) -> Order:
        raw_items = ((t.get("orders") or {}).get("order")) or []
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        items = [
            OrderItem(
                sku=str(i.get("outer_sku_id") or i.get("num_iid") or ""),
                product_name=str(i.get("title") or ""),
                quantity=int(float(i.get("num") or 1)),
                unit_price=float(i.get("price") or 0),
                subtotal=float(i.get("price") or 0) * int(float(i.get("num") or 1)),
            )
            for i in raw_items
        ]
        return Order(
            order_no=str(t.get("tid") or ""),
            user_id=0,  # 平台单不属本地 user 表，归属靠 buyer 绑定关系表达
            status=to_dial_status(str(t.get("status") or "")),
            items=items,
            total_amount=float(t.get("payment") or 0),
            receiver_name=t.get("receiver_name"),
            receiver_phone=t.get("receiver_mobile"),
            create_time=_parse_time(t.get("created")),
            update_time=_parse_time(t.get("modified") or t.get("created")),
            channel=self.name,
        )
