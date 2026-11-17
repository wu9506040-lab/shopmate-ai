"""渠道聚合 —— 把「查订单」从单库查询升级为多源查询。

核心语义变化（这就是本次改造的动机）：
    旧：本地库查不到 → 直接答「您没有订单」。但商家同时在淘宝/抖店卖货，
        买家说的那单可能在别的平台，客服系统等于睁眼说瞎话。
    新：本地 + 各平台**并发**查一遍；只有所有渠道都查通且都是 0 单，
        才能说「确实没有订单」。有渠道没查成，就必须如实说明是没查到、
        而不是没有 —— 这两种结论对客服是完全不同的行动指引。

并发而非串行：local 是毫秒级 DB 查询，外部走 HTTP，gather 后总耗时取决于
最慢的那个渠道（受 ORDER_CHANNEL_TIMEOUT 限制），不会把几个渠道的延迟累加。

探测结果靠**显式返回**而不是 ContextVar：调用链里存在 asyncio.run / 线程池桥接，
Task 会复制一份 context，协程内 set 的 ContextVar 传不回外层（实测读不到）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Tuple

from app.core.config import settings
from app.schemas.business import Order
from app.services.order.channels.base import (
    BuyerRef,
    ChannelQueryResult,
    ChannelStatus,
    OrderChannel,
)
from app.services.order.channels.binding import load_platform_ids
from app.services.order.channels.registry import build_channels
from app.services.order.factory import run_sync

logger = logging.getLogger(__name__)

_CHANNEL_ZH = {
    "local": "自有商城",
    "taobao": "淘宝",
    "douyin": "抖店",
    "pdd": "拼多多",
}

# 用户口语里怎么称呼各平台（点名平台时要能对上）
_CHANNEL_ALIASES = {
    "local": ("自有商城", "官网", "小程序", "商城"),
    "taobao": ("淘宝", "天猫", "taobao"),
    "douyin": ("抖音", "抖店", "douyin"),
    "pdd": ("拼多多", "pdd"),
}


def channel_zh(name: str) -> str:
    return _CHANNEL_ZH.get(name, name)


def detect_mentioned_channels(query: str) -> list[str]:
    """用户这句话里点名了哪些平台。

    「我在淘宝买的那件能退吗」这种问法，如果只回「已查过自有商城没查到」，
    等于答非所问——用户已经告诉你单在哪个平台了。要么去那个渠道查，
    要么如实说那个渠道还没接入。
    """
    q = query or ""
    hits: list[str] = []
    for name, aliases in _CHANNEL_ALIASES.items():
        if any(a in q for a in aliases):
            hits.append(name)
    return hits


def probe_summary(results: List[ChannelQueryResult]) -> dict:
    """把探测结果压成审计用的摘要。"""
    if not results:
        return {"queried": [], "unavailable": [], "total_orders": 0}
    return {
        "queried": [r.channel for r in results if r.status == ChannelStatus.OK],
        "unavailable": [
            {"channel": r.channel, "status": r.status.value, "detail": r.detail}
            for r in results
            if r.status != ChannelStatus.OK
        ],
        "total_orders": sum(len(r.orders) for r in results),
    }


def describe_zero_orders(
    results: Optional[List[ChannelQueryResult]] = None, query: str = "",
) -> str:
    """0 单时给用户看的话术 —— 必须区分「查过了没有」和「没查成」。

    「您没有订单」是断言；「我没查到」是事实。客服照后者去核实，
    照前者就会直接挂断一个其实有单的买家。

    query 用来识别用户点名的平台：「我在淘宝买的」而淘宝渠道没接入时，
    必须直接说淘宝那边查不了，否则等于答非所问。
    """
    results = results or []

    ok = [r for r in results if r.status == ChannelStatus.OK]
    unbound = [r for r in results if r.status == ChannelStatus.UNBOUND]
    failed = [r for r in results if r.status in (ChannelStatus.ERROR, ChannelStatus.TIMEOUT)]
    disabled = [r for r in results if r.status == ChannelStatus.DISABLED]

    parts: list[str] = []
    if ok:
        parts.append("已查过" + "、".join(channel_zh(r.channel) for r in ok) + "，没有查到订单记录")
    if unbound:
        parts.append("、".join(channel_zh(r.channel) for r in unbound) + "账号还没绑定，这部分查不到")
    if failed:
        parts.append("、".join(channel_zh(r.channel) for r in failed) + "接口暂时不通，未能确认")
    if disabled:
        # 未接入的渠道要单独说明：买家说「我在淘宝买的」时，客服得知道
        # 淘宝压根没接通，而不是以为查过了真的没有。
        parts.append("、".join(channel_zh(r.channel) for r in disabled) + "渠道尚未接入")

    # 用户点名了某个平台，但该渠道压根没在这次探测里出现（未配置到 ORDER_CHANNELS）
    mentioned = detect_mentioned_channels(query)
    known = {r.channel for r in results}
    missing = [m for m in mentioned if m not in known]
    if missing:
        parts.append(
            "您提到的" + "、".join(channel_zh(m) for m in missing)
            + "订单，那边还没有接入查询"
        )

    text = "；".join(parts) if parts else "我这边暂时没查到您的订单记录"
    if failed or unbound or disabled or missing:
        return text + "。方便的话把订单号发我，我按单号再核一次。"
    return text + "。如果刚下单，可能还没同步，稍等几分钟再问我就行。"


class ChannelOrderService:
    """OrderService Protocol 的多渠道聚合实现。

    对外接口与 MySQLOrderService 完全一致，所以 OrderTool / 各 handler 无需改动。
    """

    def __init__(self, channels: Optional[List[OrderChannel]] = None) -> None:
        self._channels: List[OrderChannel] = (
            channels if channels is not None else build_channels()
        )
        base_timeout = float(getattr(settings, "ORDER_CHANNEL_TIMEOUT", 2.0) or 2.0)
        # 外壳比渠道内部超时多给 0.5s，避免外层先杀掉内层本该正常返回的请求
        self._deadline = base_timeout + 0.5

    @property
    def channel_names(self) -> list[str]:
        return [c.name for c in self._channels]

    # ---------- 探测 ----------
    async def probe(
        self, buyer: BuyerRef, status: Optional[str] = None, limit: int = 20,
    ) -> List[ChannelQueryResult]:
        """并发查所有渠道。未配置的外部渠道不发请求，直接标记 DISABLED。"""
        tasks: dict[str, asyncio.Future] = {}
        results: List[ChannelQueryResult] = []

        for ch in self._channels:
            if ch.name != "local" and not ch.is_configured():
                results.append(
                    ChannelQueryResult(ch.name, ChannelStatus.DISABLED, [], "未配置凭证")
                )
                continue
            tasks[ch.name] = asyncio.ensure_future(self._safe_query(ch, buyer, status, limit))

        if tasks:
            done = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for name, r in zip(list(tasks.keys()), done):
                if isinstance(r, BaseException):
                    results.append(ChannelQueryResult(name, ChannelStatus.ERROR, [], str(r)))
                else:
                    results.append(r)

        order_idx = {c.name: i for i, c in enumerate(self._channels)}
        results.sort(key=lambda r: order_idx.get(r.channel, 99))
        return results

    async def _safe_query(
        self, ch: OrderChannel, buyer: BuyerRef, status: Optional[str], limit: int,
    ) -> ChannelQueryResult:
        try:
            return await asyncio.wait_for(
                ch.list_user_orders(buyer, status=status, limit=limit),
                timeout=self._deadline,
            )
        except asyncio.TimeoutError:
            return ChannelQueryResult(
                ch.name, ChannelStatus.TIMEOUT, [], f"超时(>{self._deadline:.1f}s)"
            )
        except Exception as e:  # noqa: BLE001 —— 任何渠道异常都不能打挂主链路
            logger.warning(f"channel {ch.name} query failed: {e}")
            return ChannelQueryResult(ch.name, ChannelStatus.ERROR, [], str(e))

    async def probe_and_list(
        self, user_id: int, status: Optional[str] = None, limit: int = 20,
    ) -> Tuple[List[Order], List[ChannelQueryResult]]:
        """查订单 + 同时把各渠道探测状态带出来（0 单话术要用）。"""
        buyer = BuyerRef(user_id=user_id, platform_ids=load_platform_ids(user_id))
        results = await self.probe(buyer, status=status, limit=limit)
        return self._merge(results), results

    # ---------- OrderService Protocol ----------
    async def list_user_orders(
        self, user_id: int, status: Optional[str] = None,
        start_date: Optional[datetime] = None, end_date: Optional[datetime] = None,
        limit: int = 20, cursor: Optional[str] = None,
    ) -> tuple[List[Order], Optional[str]]:
        orders, _ = await self.probe_and_list(user_id, status=status, limit=limit)
        # 时间窗过滤在聚合层统一做（各平台时间字段语义/时区不一致，
        # 交给渠道自己过滤容易漏单，统一在归一化后的 DTO 上截更可控）
        if start_date:
            orders = [o for o in orders if o.create_time and o.create_time >= start_date]
        if end_date:
            orders = [o for o in orders if o.create_time and o.create_time <= end_date]
        # 聚合后不跨渠道分页：各平台 cursor 互不通用，翻页语义会错乱
        return orders[:limit], None

    async def get_order(self, user_id: int, order_no: str) -> Optional[Order]:
        buyer = BuyerRef(user_id=user_id, platform_ids=load_platform_ids(user_id))
        if not order_no:
            return None
        order_idx = {c.name: i for i, c in enumerate(self._channels)}
        candidates = [c for c in self._channels if c.name == "local" or c.is_configured()]
        if not candidates:
            return None

        async def _get(ch: OrderChannel) -> Optional[Order]:
            try:
                return await asyncio.wait_for(ch.get_order(buyer, order_no), timeout=self._deadline)
            except Exception as e:  # noqa: BLE001
                logger.info(f"channel {ch.name} get_order failed: {e}")
                return None

        found = await asyncio.gather(*[_get(c) for c in candidates], return_exceptions=True)
        hits = [o for o in found if isinstance(o, Order)]
        if not hits:
            return None
        # 多平台订单号理论上不撞，真撞了按渠道注册顺序取（local 优先）
        hits.sort(key=lambda o: order_idx.get(o.channel, 99))
        return hits[0]

    async def get_order_status(self, order_no: str) -> Optional[str]:
        # Protocol 无 user_id，只用于内部状态刷新；沿用本地语义即可
        for ch in self._channels:
            if ch.name != "local":
                continue
            svc = getattr(ch, "_svc", None)
            if svc is None:
                continue
            try:
                return await svc.get_order_status(order_no)
            except Exception as e:  # noqa: BLE001
                logger.info(f"get_order_status failed: {e}")
                return None
        return None

    # ---------- 内部 ----------
    @staticmethod
    def _merge(results: List[ChannelQueryResult]) -> List[Order]:
        """合并去重。results 已按渠道注册顺序排好（local 在前），
        所以同一 order_no 出现在多渠道时优先保留本地那条。"""
        seen: dict[str, Order] = {}
        for r in results:
            for o in r.orders:
                key = o.order_no or ""
                if key and key not in seen:
                    seen[key] = o
        merged = list(seen.values())
        merged.sort(key=lambda o: o.create_time or datetime.min, reverse=True)
        return merged


# =============================================================
# 工厂入口（懒加载单例；测试可 reset）
# =============================================================
_service: Optional[ChannelOrderService] = None


def get_channel_order_service() -> ChannelOrderService:
    global _service
    if _service is None:
        _service = ChannelOrderService()
    return _service


def reset_channel_order_service() -> None:
    """测试钩子：重置单例（渠道配置变更 / 单测隔离）。"""
    global _service
    _service = None


def probe_channel_orders(
    user_id: int, status: Optional[str] = None, limit: int = 20,
) -> Tuple[List[Order], List[ChannelQueryResult]]:
    """同步入口：查订单并带出各渠道状态。

    决策层（OrderContextResolver）是 sync 的，走这里拿探测结果，
    才能在 0 单时区分「确实没有」和「有渠道没查成」。
    """
    return run_sync(get_channel_order_service().probe_and_list(user_id, status=status, limit=limit))
