"""追问句意图继承单测

背景：意图分类只看当前这一句。"那防水吗"没有主语、没有领域词，单句判就是闲聊。
guard 的 L2 已经按 has_history 放宽（BUGS 36），意图层是同一个坑的另一半。

边界比能力重要：宁可漏救，也不能把"谢谢"拉去查商品。
"""
from unittest.mock import patch

import pytest

from app.services.chat import orchestrator as o
from app.services.context.context_service import ConversationContext


def _ctx(last_intent):
    return ConversationContext(session_id="s1", user_id=42, last_intent=last_intent)


def _run(query, primary, last_intent, session_id="s1", user_id=42):
    with patch.object(o, "get_context_service") as m:
        m.return_value.load.return_value = _ctx(last_intent)
        return o._inherit_intent_on_followup(query, primary, session_id, user_id)


class TestInherit:

    def test_followup_inherits_product(self):
        assert _run("那防水吗", "chitchat", "product_query") == "product_query"

    def test_followup_inherits_policy(self):
        assert _run("那运费呢", "chitchat", "policy_query") == "policy_query"

    def test_followup_inherits_order(self):
        assert _run("那单呢", "chitchat", "order_query") == "order_query"

    def test_followup_inherits_refund(self):
        assert _run("那能退吗", "chitchat", "refund_query") == "refund_query"


class TestBoundaries:

    def test_pure_thanks_stays_chitchat(self):
        assert _run("谢谢", "chitchat", "product_query") == "chitchat"

    def test_thanks_with_filler_stays_chitchat(self):
        assert _run("好的谢谢啦", "chitchat", "product_query") == "chitchat"

    def test_greeting_stays_chitchat(self):
        assert _run("在吗在吗", "chitchat", "product_query") == "chitchat"

    def test_no_history_stays_chitchat(self):
        assert _run("那防水吗", "chitchat", "product_query", session_id=None) == "chitchat"

    def test_last_chitchat_stays_chitchat(self):
        assert _run("那防水吗", "chitchat", "chitchat") == "chitchat"

    def test_non_business_last_intent_ignored(self):
        assert _run("那防水吗", "chitchat", "blocked") == "chitchat"

    def test_business_intent_not_overridden(self):
        """只救 chitchat 误判，业务意图之间不互相改"""
        assert _run("那能退吗", "order_query", "refund_query") == "order_query"

    def test_short_query_not_inherited(self):
        assert _run("呢", "chitchat", "product_query") == "chitchat"

    def test_db_error_falls_back(self):
        with patch.object(o, "get_context_service") as m:
            m.return_value.load.side_effect = RuntimeError("db down")
            assert o._inherit_intent_on_followup(
                "那防水吗", "chitchat", "s1", 42
            ) == "chitchat"
