"""政策问句改判测试。

背景：60 条多轮对话实测里 16 条答成「您当前没有订单」，全部是
「退货运费谁出」「七天无理由怎么算」「那退货呢」这类**规则问题**被
分类成 refund_query，然后被订单解析吃掉的。这份测试守住那条护栏。

判定原则（错了代价很不对称）：
- 漏改：还是走订单链路 —— 现在查不到也会如实说「没查到」，可接受
- 错改：把真退款请求当政策问答 —— 用户拿不到人工，不可接受
所以个人锚点的判定必须宁可多认。
"""
from __future__ import annotations

import pytest

from app.services.chat.order_scope import (
    has_order_no,
    has_personal_reference,
    is_policy_style,
    should_reroute_to_policy,
)


class TestPersonalReference:
    @pytest.mark.parametrize("q", [
        "我这单能退吗",
        "我这个订单什么时候到",
        "我上周买的夹克想退",
        "帮我查一下我的订单",
        "我买的衣服有质量问题",
        "退到我的支付宝",
        "ORD2026111637378A 能退吗",
        "3456789012345678901 这个单退了吗",
    ])
    def test_has_personal_anchor(self, q):
        assert has_personal_reference(q) is True

    @pytest.mark.parametrize("q", [
        "退货运费谁出",
        "七天无理由怎么算",
        "售后政策是什么",
        "能开发票吗",
    ])
    def test_policy_question_has_no_anchor(self, q):
        assert has_personal_reference(q) is False

    def test_order_no_patterns(self):
        assert has_order_no("ORD2026111637378A") is True
        assert has_order_no("3456789012345678901") is True
        assert has_order_no("退货运费谁出") is False


class TestPolicyStyle:
    @pytest.mark.parametrize("q", [
        "退货运费谁出",
        "七天无理由怎么算",
        "多久能到账",
        "有什么条件",
        "需要什么材料",
        "流程是怎样的",
        "那退货呢",
        "为什么",
    ])
    def test_policy_like(self, q):
        assert is_policy_style(q) is True


class TestReroute:
    @pytest.mark.parametrize("q", [
        "退货运费谁出",          # 用户工单里被误判成 P0 质量问题的那句
        "七天无理由怎么算",
        "售后退换规则是什么",
        "那退货呢",              # 多轮追问
        "还有别的情况吗",
        "为什么",
        "三包政策是怎样的",
    ])
    def test_policy_question_rerouted(self, q):
        assert should_reroute_to_policy(q, "refund_query") is True

    @pytest.mark.parametrize("q", [
        "我这单能退吗",
        "我要退款",
        "我上周买的夹克要退货",
        "ORD2026111637378A 我要退",
    ])
    def test_personal_request_not_rerouted(self, q):
        assert should_reroute_to_policy(q, "refund_query") is False

    def test_locked_order_blocks_reroute(self):
        """会话已锁定某一单时，「那能退吗」问的就是那一单，不能改判。"""
        assert should_reroute_to_policy("那能退吗", "refund_query", has_locked_order=True) is False

    def test_non_order_intent_untouched(self):
        """商品咨询 / 政策咨询本来就走对路径，改判不该插手。"""
        assert should_reroute_to_policy("这件夹克有货吗", "product_query") is False
        assert should_reroute_to_policy("退货政策是什么", "policy_query") is False

    def test_order_query_empty_reference(self):
        """问「为什么还没发货」但点了「我的」→ 是个人请求，不改判。"""
        assert should_reroute_to_policy("为什么我的单还没发货", "order_query") is False
        # 纯问规则 → 改判
        assert should_reroute_to_policy("发货一般多久", "order_query") is True

    def test_wo_yao_anchor_blocks_reroute(self):
        """「我要退款怎么操作」含疑问词但没有「我的」——没有「我要」锚点时
        会被误改判成政策问答，真退款请求拿不到人工。繁体同理。"""
        assert has_personal_reference("我要退款怎么操作") is True
        assert should_reroute_to_policy("我要退款怎么操作", "refund_query") is False
        assert should_reroute_to_policy("我要退貨，可以嗎", "refund_query") is False

    def test_yaoshi_hypothetical_followup(self):
        """「那要是洗过了」这类假设式追问没有常规疑问词，漏判会掉进
        退款链路回 0 单话术（实测 60 条压测翻车）。"""
        assert should_reroute_to_policy("那要是洗过了", "refund_query") is True
        assert should_reroute_to_policy("要是剪了吊牌呢", "refund_query") is True
