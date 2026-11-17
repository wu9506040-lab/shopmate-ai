"""渠道注册表 —— 按配置装配本次运行的渠道集合。

配置：settings.ORDER_CHANNELS（逗号分隔，如 "local,taobao,douyin"）
    - local 永远保留（自有商城是主渠道，配掉了就没兜底）
    - 外部渠道即使凭证缺失也保留在列表里，只是标记 DISABLED ——
      「淘宝没配」和「淘宝没这单」对客服是完全不同的信息，不能混为一谈

新增平台（拼多多/快手/小红书）：写一个类实现 base.OrderChannel，
在 _BUILDERS 里登记，配置里加上名字即可，聚合层无需改动。
"""
from __future__ import annotations

import logging
from typing import Callable, List

from app.core.config import settings
from app.services.order.channels.base import OrderChannel
from app.services.order.channels.douyin import DouyinChannel
from app.services.order.channels.local import LocalMallChannel
from app.services.order.channels.taobao import TaobaoChannel

logger = logging.getLogger(__name__)

_BUILDERS: dict[str, Callable[[], OrderChannel]] = {
    "local": LocalMallChannel,
    "taobao": TaobaoChannel,
    "douyin": DouyinChannel,
}


def configured_channel_names() -> list[str]:
    """读配置里启用的渠道名（local 强制排首位，聚合时本地单优先）。"""
    raw = getattr(settings, "ORDER_CHANNELS", "local") or "local"
    if isinstance(raw, (list, tuple)):
        names = [str(x).strip().lower() for x in raw]
    else:
        names = [x.strip().lower() for x in str(raw).split(",")]
    names = [n for n in names if n]
    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    # local 必须在首位：本地单是主数据源，聚合去重时优先保留它；
    # 配成 "taobao,local" 也要纠正过来，否则同一单号会优先保留平台那条。
    if "local" in ordered:
        ordered.remove("local")
    ordered.insert(0, "local")
    return ordered


def build_channels() -> List[OrderChannel]:
    """装配渠道实例。未登记的名字只记一条 warning，不影响主流程。"""
    channels: List[OrderChannel] = []
    for name in configured_channel_names():
        builder = _BUILDERS.get(name)
        if builder is None:
            logger.warning(f"ORDER_CHANNELS 含未登记渠道: {name}（已忽略）")
            continue
        try:
            ch = builder()
        except Exception as e:  # noqa: BLE001 —— 单个渠道装配失败不能拖死启动
            logger.warning(f"渠道 {name} 装配失败: {e}")
            continue
        channels.append(ch)
        if name != "local" and not ch.is_configured():
            logger.info(f"渠道 {name} 已登记但未配置凭证，查询时按 DISABLED 处理")
    return channels
