"""写侧编排单测（turn_recorder）

验证「归属校验在写入口，不在读出口」这条约定：
- 订单号格式对但查无此单 / 不属于该用户 → 不写进上下文
- 匿名用户 / 开关关闭 → 直接跳过，不查库
- 异常不影响主链路（返回 False，不抛）
- 服装事实：本轮原话 > 改口修正 > history 补偿（只填空 key）
"""
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.services.context import turn_recorder as tr
from app.services.context.context_service import ConversationContext


@pytest.fixture(autouse=True)
def _store_on(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_CONTEXT_STORE", True)


class _Recorder:
    def __init__(self, facts=None):
        self.calls = []
        self.facts = facts or {}

    def load(self, session_id, user_id):
        return ConversationContext(session_id=session_id, user_id=user_id, facts=self.facts)

    def record_turn(self, **kwargs):
        self.calls.append(kwargs)
        return True


def _run(monkeypatch, recorder=None, **kwargs):
    rec = recorder or _Recorder()
    monkeypatch.setattr(tr, "get_context_service", lambda: rec)
    params = {
        "session_id": "s1",
        "user_id": 42,
        "query": "那能退吗",
        "history": [{"role": "user", "content": "ORD20260622003 到哪了"}],
        "intent_snap": {"primary": "refund_query", "entities": {}},
        "order_no_param": None,
    }
    params.update(kwargs)
    return tr.record_turn_context(**params), rec


class TestRecordTurnContext:

    def test_owned_order_recorded(self, monkeypatch):
        with patch.object(tr.OrderTool, "get_order_by_no", return_value={"order_no": "x"}):
            ok, rec = _run(monkeypatch)
        assert ok is True
        call = rec.calls[0]
        assert call["order_no"] == "ORD20260622003"
        assert call["order_no_owned"] is True
        assert call["last_intent"] == "refund_query"

    def test_foreign_order_not_marked_owned(self, monkeypatch):
        with patch.object(tr.OrderTool, "get_order_by_no", return_value=None):
            ok, rec = _run(monkeypatch)
        assert ok is True
        assert rec.calls[0]["order_no_owned"] is False

    def test_order_check_error_treated_as_not_owned(self, monkeypatch):
        with patch.object(tr.OrderTool, "get_order_by_no", side_effect=RuntimeError("db down")):
            _ok, rec = _run(monkeypatch)
        assert rec.calls[0]["order_no_owned"] is False

    def test_no_order_no_no_db_query(self, monkeypatch):
        """没有任何订单号 → 不该为归属校验白查一次库"""
        with patch.object(tr.OrderTool, "get_order_by_no") as m:
            _ok, rec = _run(monkeypatch, history=[{"role": "user", "content": "你好"}], query="你好")
        m.assert_not_called()
        assert rec.calls[0]["order_no"] is None

    def test_anonymous_skipped(self, monkeypatch):
        ok, rec = _run(monkeypatch, user_id=0)
        assert ok is False and rec.calls == []

    def test_switch_off_skipped(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLE_CONTEXT_STORE", False)
        ok, rec = _run(monkeypatch)
        assert ok is False and rec.calls == []

    def test_exception_swallowed(self, monkeypatch):
        class Boom:
            def load(self, **_k):
                raise RuntimeError("表锁了")

            def record_turn(self, **_k):
                raise RuntimeError("表锁了")

        monkeypatch.setattr(tr, "get_context_service", lambda: Boom())
        ok = tr.record_turn_context(
            session_id="s1", user_id=42, query="你好", history=[],
            intent_snap={"primary": "chitchat", "entities": {}},
        )
        assert ok is False


class TestClothingFacts:
    """服装事实写侧：抽得准、改得掉、补得上"""

    def test_facts_from_this_turn(self, monkeypatch):
        with patch.object(tr.OrderTool, "get_order_by_no", return_value=None):
            _ok, rec = _run(monkeypatch, query="我身高170体重60穿什么码", history=[])
        assert rec.calls[0]["facts"] == {"height_cm": 170, "weight_kg": 60.0}

    def test_nothing_extracted_passes_none(self, monkeypatch):
        with patch.object(tr.OrderTool, "get_order_by_no", return_value=None):
            _ok, rec = _run(monkeypatch, query="那能退吗", history=[])
        assert rec.calls[0]["facts"] is None

    def test_correction_overwrites_known(self, monkeypatch):
        """「我记错了，是175」：裸数字正常规则抓不到，靠改口规则覆盖"""
        rec = _Recorder(facts={"height_cm": {"value": 170}})
        with patch.object(tr.OrderTool, "get_order_by_no", return_value=None):
            _ok, rec = _run(monkeypatch, rec, query="我记错了，是175", history=[])
        assert rec.calls[0]["facts"] == {"height_cm": 175}

    def test_history_only_fills_missing_keys(self, monkeypatch):
        """库里已有 height=170，history 里也是 170 → 不该被当成新事实重复写"""
        rec = _Recorder(facts={"height_cm": {"value": 170}})
        history = [{"role": "user", "content": "我身高170"}]
        with patch.object(tr.OrderTool, "get_order_by_no", return_value=None):
            _ok, rec = _run(monkeypatch, rec, query="那能退吗", history=history)
        assert rec.calls[0]["facts"] is None

    def test_history_fills_when_library_empty(self, monkeypatch):
        """首轮写入丢失（会话未落库）→ 下一轮从 history 补回来"""
        history = [{"role": "user", "content": "我身高170体重60kg"}]
        with patch.object(tr.OrderTool, "get_order_by_no", return_value=None):
            _ok, rec = _run(monkeypatch, query="那能退吗", history=history)
        assert rec.calls[0]["facts"] == {"height_cm": 170, "weight_kg": 60.0}

    def test_assistant_text_not_used_for_facts(self, monkeypatch):
        """尺码表是助手复述的，不能当成买家尺码"""
        history = [
            {"role": "user", "content": "有什么码"},
            {"role": "assistant", "content": "尺码表：S/M/L/XL 都有货，170~180cm 选 XL"},
        ]
        with patch.object(tr.OrderTool, "get_order_by_no", return_value=None):
            _ok, rec = _run(monkeypatch, None, query="那能退吗", history=history)
        assert rec.calls[0]["facts"] is None
