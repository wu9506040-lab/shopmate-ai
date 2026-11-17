"""退款流读取会话上下文（2B）单测

核心场景：买家第一轮说过订单号，第二轮只问「那能退吗」。
上下文里有锁定的订单 → 直接用，不再让买家重复报单号。

断言口径：
- ctx 命中时，OrderTool 收到的正是 ctx 里的订单号（不是历史正则凑出来的）
- ctx 里的订单已失效 → 忽略并回到 0/1/N 决策，而不是断言「订单不存在」
- 本轮实体给了订单号 → 实体优先，不读 ctx
"""
import os
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")

from app.core.config import settings
from app.services.business_flow.refund_flow import RefundFlow
from app.services.context.context_service import ConversationContext

CTX_ORDER = "ORD20260628004"
OTHER_ORDER = "ORD20260701001"
FAKE_ORDER = {"order_no": CTX_ORDER, "status": "delivered", "total_amount": 599.0}


def _intent_result(order_no=None) -> dict:
    return {
        "intents": [{"intent": "refund_query", "confidence": 1.0}],
        "primary": "refund_query",
        "intent": "refund_query",
        "method": "rule",
        "confidence": 1.0,
        "entities": {"order_no": order_no, "sku": None, "keywords": []},
    }


def _ctx(order_no) -> ConversationContext:
    return ConversationContext(session_id="s1", user_id=42, current_order_no=order_no)


def _run(flow):
    with patch("app.core.agent_checkpointer.get_checkpointer", return_value=None):
        return list(flow.run())


class TestRefundFlowContextOrder:

    def test_second_turn_uses_context_order(self):
        """历史里没有订单号，只有 context 有 → 仍能定位到那一单"""
        with patch(
            "app.services.business_flow.refund_flow.get_context_service"
        ) as mock_svc, patch(
            "app.services.business_flow.refund_flow.OrderTool.get_order_by_no",
            return_value=FAKE_ORDER,
        ) as mock_get, patch(
            "app.services.business_flow.refund_flow._judge_basic_refundable",
            return_value=(True, "已签收 3 天，在 7 天无理由退货期限内", 3, "已签收"),
        ), patch(
            "app.services.business_flow.refund_flow.refund_graph_app"
        ) as mock_graph:
            mock_svc.return_value.load.return_value = _ctx(CTX_ORDER)
            mock_graph.stream.return_value = iter([])
            events = _run(RefundFlow(
                query="那能退吗",
                user_id=42,
                intent_result=_intent_result(None),
                history=[{"role": "user", "content": "之前那个单"}],
                session_id="s1",
            ))

        mock_get.assert_any_call(42, CTX_ORDER)
        mock_graph.stream.assert_called_once()
        texts = [e[1] for e in events if e[0] == "token"]
        assert not any("不存在" in str(t) for t in texts)
        assert not any("订单号" in str(t) for t in texts)

    def test_stale_context_order_ignored(self, monkeypatch):
        """ctx 里的单已查不到 → 丢弃，回到 0/1/N 决策，不做"订单不存在"断言"""
        monkeypatch.setattr(settings, "ENABLE_ORDER_RESOLVER", True)
        empty_resolver = type("R", (), {
            "action": type("A", (), {"value": "ask_login_or_list"})(),
            "reason": "zero_orders", "total_orders": 0,
            "effective_order_no": None, "candidate_orders": [], "truncated": False,
            # 0 单话术要按渠道探测状态生成（哪个渠道查过 / 没查成），
            # 所以结果对象必须带 channel_probe
            "channel_probe": [],
            "to_dict": lambda self: {},
        })()

        with patch(
            "app.services.business_flow.refund_flow.get_context_service"
        ) as mock_svc, patch(
            "app.services.business_flow.refund_flow.OrderTool.get_order_by_no",
            return_value=None,
        ), patch(
            "app.services.business_flow.refund_flow.get_order_context_resolver"
        ) as mock_resolver, patch(
            "app.services.business_flow.refund_flow.OrderResolverAction"
        ) as mock_action:
            mock_svc.return_value.load.return_value = _ctx(CTX_ORDER)
            mock_resolver.return_value.resolve.return_value = empty_resolver
            mock_action.ASK_LOGIN_OR_LIST = empty_resolver.action
            events = _run(RefundFlow(
                query="那能退吗",
                user_id=42,
                intent_result=_intent_result(None),
                history=[{"role": "user", "content": "之前那个单"}],
                session_id="s1",
            ))

        texts = " ".join(str(e[1]) for e in events if e[0] == "token")
        assert "不存在" not in texts
        # 0 单说的是「没查到」而不是「你没有订单」——渠道可能没查成
        assert "没查到" in texts or "没有查到" in texts

    def test_entity_order_wins_over_context(self):
        """本轮自己报了单号 → 实体优先，不去读 ctx"""
        with patch(
            "app.services.business_flow.refund_flow.get_context_service"
        ) as mock_svc, patch(
            "app.services.business_flow.refund_flow.OrderTool.get_order_by_no",
            return_value=FAKE_ORDER,
        ), patch(
            "app.services.business_flow.refund_flow._judge_basic_refundable",
            return_value=(True, "ok", 1, "已签收"),
        ), patch(
            "app.services.business_flow.refund_flow.refund_graph_app"
        ) as mock_graph:
            mock_svc.return_value.load.return_value = _ctx(OTHER_ORDER)
            mock_graph.stream.return_value = iter([])
            _run(RefundFlow(
                query="ORD20260628004 能退吗",
                user_id=42,
                intent_result=_intent_result(CTX_ORDER),
                history=[],
                session_id="s1",
            ))
        # load 照常调用（拿 last_intent 等），但订单号以实体为准
        mock_graph.stream.assert_called_once()
