"""订单多渠道查询层测试。

覆盖四件事：
1. 平台签名（淘宝 TOP md5 / 抖店 md5）—— 签错就被平台拒绝，必须逐字符可核
2. 未配置凭证时绝不发请求（否则每次对话都白等一次 2s 超时）
3. 聚合语义：本地 0 单 + 平台有单 → 能查出来；某渠道超时/报错不能拖垮其它渠道
4. 0 单话术：必须区分「查过了没有」和「有渠道没查成」
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Optional

import pytest

from app.schemas.business import Order
from app.services.order.channels.aggregator import (
    ChannelOrderService,
    describe_zero_orders,
    probe_summary,
)
from app.services.order.channels.base import (
    BuyerRef,
    ChannelQueryResult,
    ChannelStatus,
)
from app.services.order.channels.douyin import DouyinChannel, sign_doudian
from app.services.order.channels.registry import configured_channel_names
from app.services.order.channels.taobao import TaobaoChannel, sign_top

NOW = datetime.datetime(2026, 11, 10, 12, 0, 0)


def _order(no: str, channel: str = "local", amount: float = 199.0) -> Order:
    return Order(
        order_no=no, user_id=0, status="paid", items=[], total_amount=amount,
        create_time=NOW, update_time=NOW, channel=channel,
    )


class _FakeChannel:
    """可编程假渠道：按 name 返回预设结果，并记录调用次数。"""

    def __init__(self, name: str, *, orders=None, status=ChannelStatus.OK,
                 configured=True, requires_binding=False, delay=0.0):
        self.name = name
        self._orders = orders or []
        self._status = status
        self._configured = configured
        self._requires_binding = requires_binding
        self._delay = delay
        self.calls = 0

    def is_configured(self) -> bool:
        return self._configured

    async def list_user_orders(self, buyer, status=None, limit=20):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._requires_binding and not buyer.platform_id(self.name):
            return ChannelQueryResult(self.name, ChannelStatus.UNBOUND, [], "未绑定")
        return ChannelQueryResult(self.name, self._status, list(self._orders))

    async def get_order(self, buyer, order_no):
        self.calls += 1
        for o in self._orders:
            if o.order_no == order_no:
                return o
        return None


# =============================================================
# 1. 签名纯函数
# =============================================================
class TestSignatures:
    def test_taobao_sign_is_deterministic_and_case_upper(self):
        params = {
            "method": "taobao.trades.sold.get",
            "app_key": "12345",
            "timestamp": "2026-11-17 20:00:00",
            "v": "2.0",
            "format": "json",
            "sign_method": "md5",
            "fields": "tid,status",
        }
        s1 = sign_top(params, "secret")
        s2 = sign_top(dict(reversed(list(params.items()))), "secret")
        assert s1 == s2              # 与参数字典顺序无关（内部排序）
        assert s1 == s1.upper()
        assert len(s1) == 32

    def test_taobao_sign_ignores_empty_and_sign_key(self):
        base = {"a": "1", "b": "2"}
        with_junk = {**base, "sign": "XXX", "empty": ""}
        assert sign_top(base, "s") == sign_top(with_junk, "s")

    def test_taobao_sign_secret_matters(self):
        assert sign_top({"a": "1"}, "s1") != sign_top({"a": "1"}, "s2")

    def test_doudian_sign_lowercase(self):
        params = {
            "app_key": "ak", "method": "order.searchList",
            "param_json": '{"shop_id":"123"}',
            "timestamp": "2026-11-17 20:00:00", "v": "2", "sign_method": "md5",
        }
        s = sign_doudian(params, "as")
        assert s == s.lower()
        assert len(s) == 32
        # 换 secret 必变
        assert s != sign_doudian(params, "other")


# =============================================================
# 2. 未配置凭证 → DISABLED 且不发请求
# =============================================================
class TestUnconfiguredChannel:
    def test_taobao_without_credentials(self):
        ch = TaobaoChannel(app_key="", app_secret="", session_key="")
        assert ch.is_configured() is False

    def test_douyin_without_credentials(self):
        ch = DouyinChannel(app_key="ak", app_secret="as", shop_id="")
        assert ch.is_configured() is False   # 缺 shop_id

    def test_unconfigured_channel_is_skipped_not_called(self):
        """未配置的渠道必须连请求都不发 —— 否则每轮对话白等一次超时。"""
        local = _FakeChannel("local", orders=[_order("ORD20261110001A")])
        cloud = _FakeChannel("taobao", configured=False)
        svc = ChannelOrderService([local, cloud])

        orders, probe = asyncio.run(svc.probe_and_list(1, limit=20))

        assert cloud.calls == 0
        assert [o.order_no for o in orders] == ["ORD20261110001A"]
        by_name = {r.channel: r for r in probe}
        assert by_name["taobao"].status == ChannelStatus.DISABLED
        assert by_name["local"].status == ChannelStatus.OK


# =============================================================
# 3. 聚合语义
# =============================================================
class TestAggregation:
    def test_local_empty_but_platform_has_order(self):
        """核心场景：自有商城没单，但买家在淘宝买过 —— 必须查出来。

        这就是「不确定订单就去接口查」的意义。改造前这里会直接回「您没有订单」。
        """
        local = _FakeChannel("local")
        # 未绑定平台账号 → UNBOUND：查不到是「没绑定」，不是「没订单」
        unbound = _FakeChannel("taobao", orders=[_order("3456789012345678901", "taobao")],
                               requires_binding=True)
        orders, probe = asyncio.run(ChannelOrderService([local, unbound]).probe_and_list(1))
        assert orders == []
        assert {r.channel: r.status for r in probe}["taobao"] == ChannelStatus.UNBOUND

        # 绑定之后就能查到（这是真正"去接口查"的路径）
        bound = _FakeChannel("taobao", orders=[_order("3456789012345678901", "taobao")])
        orders2, _ = asyncio.run(ChannelOrderService([local, bound]).probe_and_list(1))
        assert [o.order_no for o in orders2] == ["3456789012345678901"]
        assert orders2[0].channel == "taobao"

    def test_merge_sorts_by_create_time_desc(self):
        old = _order("ORD_OLD")
        old.create_time = datetime.datetime(2026, 1, 1)
        new = _order("ORD_NEW")
        new.create_time = datetime.datetime(2026, 11, 1)
        svc = ChannelOrderService([
            _FakeChannel("local", orders=[old, new]),
        ])
        orders, _ = asyncio.run(svc.probe_and_list(1))
        assert [o.order_no for o in orders] == ["ORD_NEW", "ORD_OLD"]

    def test_dedup_keeps_first_channel(self):
        """同一单号出现在多渠道时保留先注册的（local 优先）。"""
        dup_local = _order("SAME", "local")
        dup_tb = _order("SAME", "taobao")
        svc = ChannelOrderService([
            _FakeChannel("local", orders=[dup_local]),
            _FakeChannel("taobao", orders=[dup_tb], requires_binding=False),
        ])
        orders, _ = asyncio.run(svc.probe_and_list(1))
        assert len(orders) == 1
        assert orders[0].channel == "local"

    def test_one_channel_error_does_not_break_others(self):
        """一个平台接口挂了，其余渠道的结果必须照常返回。"""
        local = _FakeChannel("local", orders=[_order("ORD20261110001A")])
        broken = _FakeChannel("taobao", status=ChannelStatus.ERROR, requires_binding=False)
        svc = ChannelOrderService([local, broken])

        orders, probe = asyncio.run(svc.probe_and_list(1))
        assert [o.order_no for o in orders] == ["ORD20261110001A"]
        assert {r.channel: r.status for r in probe}["taobao"] == ChannelStatus.ERROR

    def test_channel_exception_is_contained(self):
        class _Boom(_FakeChannel):
            async def list_user_orders(self, buyer, status=None, limit=20):
                raise RuntimeError("接口炸了")

        svc = ChannelOrderService([
            _FakeChannel("local", orders=[_order("ORDX")]),
            _Boom("taobao"),
        ])
        orders, probe = asyncio.run(svc.probe_and_list(1))
        assert len(orders) == 1                       # 本地结果保住
        assert {r.channel: r.status for r in probe}["taobao"] == ChannelStatus.ERROR

    def test_channel_timeout_is_contained(self):
        """外部渠道卡住 → TIMEOUT，本地结果照常返回，总耗时受 deadline 约束。"""
        local = _FakeChannel("local", orders=[_order("ORDX")])
        slow = _FakeChannel("taobao", requires_binding=False, delay=5.0)
        svc = ChannelOrderService([local, slow])
        svc._deadline = 0.2  # 缩短 deadline 便于测试

        orders, probe = asyncio.run(svc.probe_and_list(1))
        assert len(orders) == 1
        assert {r.channel: r.status for r in probe}["taobao"] == ChannelStatus.TIMEOUT

    def test_unbound_reported_not_silent(self):
        """没绑定平台账号 → UNBOUND，话术能说清「这部分查不到」。"""
        svc = ChannelOrderService([
            _FakeChannel("taobao", orders=[_order("TB1", "taobao")], requires_binding=True),
        ])
        _, probe = asyncio.run(svc.probe_and_list(1))
        assert probe[0].status == ChannelStatus.UNBOUND

    def test_limit_applied(self):
        svc = ChannelOrderService([
            _FakeChannel("local", orders=[_order(f"ORD{i}", amount=float(i)) for i in range(5)]),
        ])
        # limit 截断在 Protocol 方法上生效（probe_and_list 是内部接口，返回全量）
        orders, _ = asyncio.run(svc.list_user_orders(1, limit=2))
        assert len(orders) == 2

    def test_get_order_across_channels(self):
        svc = ChannelOrderService([
            _FakeChannel("local"),
            _FakeChannel("taobao", orders=[_order("TB123", "taobao")], requires_binding=False),
        ])
        got = asyncio.run(svc.get_order(1, "TB123"))
        assert got is not None and got.channel == "taobao"
        assert asyncio.run(svc.get_order(1, "NOT_EXIST")) is None


# =============================================================
# 4. 0 单话术 / 摘要
# =============================================================
class TestZeroOrderMessaging:
    def _r(self, channel, status, detail=""):
        return ChannelQueryResult(channel, status, [], detail)

    def test_all_channels_ok_but_empty(self):
        msg = describe_zero_orders([self._r("local", ChannelStatus.OK)])
        assert "没有查到订单" in msg
        assert "自有商城" in msg
        assert "没有订单" not in msg   # 不许下断言

    def test_platform_failed_says_unconfirmed(self):
        """接口不通时不能说「没有」，只能说「未能确认」并要单号。"""
        msg = describe_zero_orders([
            self._r("local", ChannelStatus.OK),
            self._r("taobao", ChannelStatus.ERROR, "HTTP 500"),
        ])
        assert "未能确认" in msg
        assert "订单号" in msg

    def test_unbound_says_not_bound(self):
        msg = describe_zero_orders([
            self._r("local", ChannelStatus.OK),
            self._r("douyin", ChannelStatus.UNBOUND, "未绑定"),
        ])
        assert "没绑定" in msg or "未绑定" in msg
        assert "订单号" in msg

    def test_all_disabled(self):
        msg = describe_zero_orders([
            self._r("local", ChannelStatus.OK),
            self._r("taobao", ChannelStatus.DISABLED, "未配置凭证"),
        ])
        assert "尚未接入" in msg

    def test_empty_probe_still_safe(self):
        assert describe_zero_orders([]) != ""

    def test_mentioned_platform_not_integrated(self):
        """用户说「我在淘宝买的」而淘宝渠道没接入 → 必须点明，不能只回自有商城没查到。

        这类问法如果不点平台，用户会觉得客服根本没听懂他在说什么。
        """
        msg = describe_zero_orders(
            [self._r("local", ChannelStatus.OK)], query="我在淘宝买的那件能退吗",
        )
        assert "淘宝" in msg
        assert "还没有接入" in msg
        assert "订单号" in msg   # 给下一步动作

    def test_mentioned_platform_already_queried(self):
        """平台已在渠道列表里（哪怕查了没单），就不再重复说「未接入」。"""
        msg = describe_zero_orders(
            [self._r("local", ChannelStatus.OK), self._r("taobao", ChannelStatus.OK)],
            query="我在淘宝买的那件能退吗",
        )
        assert "还没有接入" not in msg
        assert "淘宝" in msg

    def test_douyin_alias(self):
        from app.services.order.channels.aggregator import detect_mentioned_channels

        assert detect_mentioned_channels("抖音上买的") == ["douyin"]
        assert detect_mentioned_channels("抖店的订单") == ["douyin"]
        assert detect_mentioned_channels("淘宝和抖音都买过") == ["taobao", "douyin"]

    def test_probe_summary_shape(self):
        summary = probe_summary([
            self._r("local", ChannelStatus.OK),
            self._r("taobao", ChannelStatus.DISABLED, "未配置凭证"),
        ])
        assert summary["queried"] == ["local"]
        assert summary["unavailable"][0]["channel"] == "taobao"
        assert summary["total_orders"] == 0


# =============================================================
# 5. 渠道配置解析
# =============================================================
class TestRegistry:
    def test_local_forced_first(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "ORDER_CHANNELS", "taobao,local", raising=False)
        assert configured_channel_names()[0] == "local"

    def test_unknown_channel_ignored(self, monkeypatch):
        from app.core.config import settings
        from app.services.order.channels.registry import build_channels

        monkeypatch.setattr(settings, "ORDER_CHANNELS", "local,unknown_shop", raising=False)
        names = [c.name for c in build_channels()]
        assert "unknown_shop" not in names
        assert "local" in names

    def test_taobao_douyin_buildable(self, monkeypatch):
        from app.core.config import settings
        from app.services.order.channels.registry import build_channels

        monkeypatch.setattr(settings, "ORDER_CHANNELS", "local,taobao,douyin", raising=False)
        names = [c.name for c in build_channels()]
        assert names[:3] == ["local", "taobao", "douyin"]


# =============================================================
# 6. 平台 DTO 映射（金额单位 / 状态归一）
# =============================================================
class TestPlatformMapping:
    def test_taobao_order_mapping(self):
        ch = TaobaoChannel(app_key="k", app_secret="s", session_key="t")
        o = ch._to_order({
            "tid": "3456789012345678901",
            "status": "WAIT_SELLER_SEND_GOODS",
            "payment": "299.00",
            "created": "2026-11-10 12:00:00",
            "modified": "2026-11-11 09:00:00",
            "orders": {"order": [{
                "title": "羊毛混纺夹克", "num": "2", "price": "149.50",
                "num_iid": "888", "outer_sku_id": "FS368998-M",
            }]},
        })
        assert o.order_no == "3456789012345678901"
        assert o.status == "paid"
        assert o.total_amount == 299.0
        assert o.channel == "taobao"
        assert o.items[0].sku == "FS368998-M"
        assert o.items[0].subtotal == 299.0

    def test_douyin_amount_in_cents(self):
        """抖店金额单位是分，必须换算，否则会答成「支付 19900 元」。"""
        ch = DouyinChannel(app_key="k", app_secret="s", shop_id="999")
        o = ch._to_order({
            "order_id": "6912345678901234567",
            "order_status": "2",
            "pay_amount": 19900,          # 分
            "create_time": 1762732800,
            "sku_order_list": [{
                "product_name": "羊毛混纺夹克", "item_num": 1,
                "goods_price": 19900, "sku_id": "FS368998-M",
            }],
        })
        assert o.total_amount == 199.0
        assert o.items[0].unit_price == 199.0
        assert o.status == "paid"
        assert o.channel == "douyin"

    def test_douyin_millisecond_timestamp(self):
        ch = DouyinChannel(app_key="k", app_secret="s", shop_id="999")
        o = ch._to_order({"order_id": "1", "order_status": "4", "create_time": 1762732800000})
        assert o.create_time.year == 2025 or o.create_time.year == 2026
