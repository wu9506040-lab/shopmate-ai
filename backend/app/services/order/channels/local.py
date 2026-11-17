"""本地商城渠道 —— 自有商城订单（MySQL）。

这一路永远启用，是主渠道。包一层 Protocol 适配，让 aggregator 能用同一套
接口对待「自有商城」和「淘宝/抖店」。
"""
from __future__ import annotations

import logging
from typing import Optional

from app.schemas.business import Order
from app.services.order.channels.base import BuyerRef, ChannelQueryResult, ChannelStatus
from app.services.order.mysql_impl import MySQLOrderService

logger = logging.getLogger(__name__)


class LocalMallChannel:
    """自有商城订单渠道（总启用）"""

    name = "local"

    def __init__(self) -> None:
        self._svc = MySQLOrderService()

    def is_configured(self) -> bool:
        return True

    async def list_user_orders(
        self, buyer: BuyerRef, status: Optional[str] = None, limit: int = 20,
    ) -> ChannelQueryResult:
        try:
            orders, _ = await self._svc.list_user_orders(
                buyer.user_id, status=status, limit=limit,
            )
            for o in orders:
                o.channel = self.name
            return ChannelQueryResult(self.name, ChannelStatus.OK, orders)
        except Exception as e:  # noqa: BLE001 —— DB 抖动不该打挂对话
            logger.warning(f"local channel list failed: user_id={buyer.user_id}, {e}")
            return ChannelQueryResult(self.name, ChannelStatus.ERROR, [], str(e))

    async def get_order(self, buyer: BuyerRef, order_no: str) -> Optional[Order]:
        try:
            o = await self._svc.get_order(buyer.user_id, order_no)
            if o:
                o.channel = self.name
            return o
        except Exception as e:  # noqa: BLE001
            logger.warning(f"local channel get failed: order_no={order_no}, {e}")
            return None
