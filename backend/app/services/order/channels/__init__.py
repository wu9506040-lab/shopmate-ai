"""订单多渠道查询层。

    local（自有商城 MySQL） + taobao + douyin + ... → aggregator 并发聚合

对外只暴露 `get_channel_order_service()`，业务层不需要知道有哪些渠道。
新增平台：实现 base.OrderChannel，在 registry._BUILDERS 登记即可。
"""
from app.services.order.channels.aggregator import (
    ChannelOrderService,
    channel_zh,
    describe_zero_orders,
    detect_mentioned_channels,
    get_channel_order_service,
    probe_channel_orders,
    probe_summary,
    reset_channel_order_service,
)
from app.services.order.channels.base import (
    BuyerRef,
    ChannelQueryResult,
    ChannelStatus,
    OrderChannel,
)
from app.services.order.channels.binding import (
    bind_platform_buyer,
    load_platform_ids,
    reset_binding_cache,
)
from app.services.order.channels.registry import build_channels, configured_channel_names

__all__ = [
    "BuyerRef",
    "ChannelQueryResult",
    "ChannelStatus",
    "ChannelOrderService",
    "OrderChannel",
    "bind_platform_buyer",
    "build_channels",
    "channel_zh",
    "configured_channel_names",
    "describe_zero_orders",
    "detect_mentioned_channels",
    "get_channel_order_service",
    "load_platform_ids",
    "probe_channel_orders",
    "probe_summary",
    "reset_binding_cache",
    "reset_channel_order_service",
]
