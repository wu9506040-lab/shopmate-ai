"""平台渠道公共件：HTTP 调用、超时、错误归一。

外部平台接口的通病：签名错、限流、参数校验失败都返回 200 + 业务错误码。
所以这里统一把「HTTP 层异常」和「业务错误码」都收敛成 `PlatformCallError`，
由调用方映射成 ChannelStatus.ERROR / TIMEOUT，不让异常穿到对话主链路。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class PlatformCallError(Exception):
    """平台接口调用失败（网络 / 超时 / 业务错误码统一收敛）"""

    def __init__(self, message: str, *, code: Optional[str] = None, timeout: bool = False):
        super().__init__(message)
        self.code = code
        self.timeout = timeout


async def post_form(
    url: str,
    data: dict[str, Any],
    *,
    timeout: float = 2.0,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """POST x-www-form-urlencoded，返 JSON dict。异常统一抛 PlatformCallError。"""
    own_client = client is None
    cli = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await asyncio.wait_for(cli.post(url, data=data), timeout=timeout)
        if resp.status_code >= 400:
            raise PlatformCallError(
                f"HTTP {resp.status_code}: {resp.text[:200]}", code=str(resp.status_code)
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise PlatformCallError(f"响应非 JSON: {resp.text[:200]!r}") from e
        return body if isinstance(body, dict) else {"_raw": body}
    except asyncio.TimeoutError as e:
        raise PlatformCallError(f"请求超时(>{timeout}s)", timeout=True) from e
    except httpx.HTTPError as e:
        raise PlatformCallError(f"网络错误: {e}") from e
    finally:
        if own_client:
            await cli.aclose()


async def get_json(
    url: str,
    params: dict[str, Any],
    *,
    timeout: float = 2.0,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """GET，返 JSON dict。"""
    own_client = client is None
    cli = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await asyncio.wait_for(cli.get(url, params=params), timeout=timeout)
        if resp.status_code >= 400:
            raise PlatformCallError(
                f"HTTP {resp.status_code}: {resp.text[:200]}", code=str(resp.status_code)
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise PlatformCallError(f"响应非 JSON: {resp.text[:200]!r}") from e
        return body if isinstance(body, dict) else {"_raw": body}
    except asyncio.TimeoutError as e:
        raise PlatformCallError(f"请求超时(>{timeout}s)", timeout=True) from e
    except httpx.HTTPError as e:
        raise PlatformCallError(f"网络错误: {e}") from e
    finally:
        if own_client:
            await cli.aclose()


def to_dial_status(platform_status: str) -> str:
    """把各平台杂乱的订单状态码归一到本系统 status 枚举。

    归一不到的一律保留原值（AI 客服按字符串消费，不认识就不会瞎解释）。
    """
    s = (platform_status or "").strip().lower()
    mapping = {
        # 淘宝 TradeStatus
        "trade_no_create": "pending",
        "trade_created": "pending",
        "wait_buyer_pay": "pending",
        "wait_seller_send_goods": "paid",
        "wait_buyer_confirm_goods": "shipped",
        "trade_finished": "completed",
        "trade_closed": "cancelled",
        "trade_refunding": "refunding",
        # 抖店 order_status
        "1": "pending",   # 待确认/待支付
        "2": "paid",      # 待发货
        "3": "shipped",   # 已发货
        "4": "completed",  # 已完成
        "5": "cancelled",  # 已关闭
        "6": "refunding",  # 售后中
    }
    return mapping.get(s, s or "unknown")
