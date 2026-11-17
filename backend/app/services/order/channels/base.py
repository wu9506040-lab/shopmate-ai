"""订单渠道协议 —— 多渠道聚合的基础契约。

为什么要有这一层：
    原来 `OrderService` 只有一个实现（本地商城 MySQL），于是「本地库里查不到」
    被直接读成「这个用户没有订单」。但商家往往同时在淘宝、抖店、拼多多开铺，
    买家说「我上周买的那单」时，那单可能压根不在自有商城库里。
    正确语义是：本地查不到 ≠ 没有订单，得再去平台接口问一遍。

设计要点：
- 每个渠道只负责「用某个平台的身份查它自己的单」，不关心聚合
- 渠道必须自带 `is_configured()`：没配凭证就是没启用，绝不发半截请求
- 查询失败（超时/签名错/限流）不得让整条链路失败 —— 由 aggregator 兜住
- 平台身份用 `BuyerRef` 传，避免各渠道各自去猜 user_id 到底指谁
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Protocol, runtime_checkable

from app.schemas.business import Order

logger = logging.getLogger(__name__)


class ChannelStatus(str, Enum):
    """单次渠道查询的结果状态（写进审计与提示语，便于运营排查）"""

    OK = "ok"              # 查通了（有没有单都算 OK）
    DISABLED = "disabled"  # 没配凭证 / 功能未开 → 这个渠道压根没查
    UNBOUND = "unbound"    # 渠道可用，但该买家没绑定平台账号
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class BuyerRef:
    """买家身份引用。

    本地商城渠道用 `user_id`；外部平台渠道用 `platform_ids` 里的平台侧 ID
    （淘宝 open_uid / 抖店 open_id）。查不到对应平台 ID 时，渠道应返 UNBOUND
    而不是拿本地 user_id 去硬查——那样查出来的单大概率不是同一个人的。
    """

    user_id: int
    platform_ids: dict[str, str] = field(default_factory=dict)

    def platform_id(self, channel: str) -> Optional[str]:
        return self.platform_ids.get(channel) or None


@dataclass
class ChannelQueryResult:
    """单个渠道的查询结果（成功/失败都走这个结构，不抛异常给上层）"""

    channel: str
    status: ChannelStatus
    orders: List[Order] = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == ChannelStatus.OK


@runtime_checkable
class OrderChannel(Protocol):
    """订单渠道协议。新增平台（拼多多/快手/小红书）只需实现这三个方法。"""

    name: str

    def is_configured(self) -> bool:
        """凭证是否齐全。False 时 aggregator 直接跳过，不发请求。"""
        ...

    async def list_user_orders(
        self, buyer: BuyerRef, status: Optional[str] = None, limit: int = 20,
    ) -> ChannelQueryResult:
        """列该买家在此渠道的订单。失败请返回对应 status，不要 raise。"""
        ...

    async def get_order(self, buyer: BuyerRef, order_no: str) -> Optional[Order]:
        """按订单号查（已由调用方校验过归属）。不存在返 None。"""
        ...
