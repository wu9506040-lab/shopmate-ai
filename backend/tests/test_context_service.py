"""ContextService 单测（DB 用假 session 替身，不连真库）

覆盖四件事：
1. 开关关闭时短路（读返空 / 写返 True）
2. 归属隔离：别人的会话上下文一律不读不写（BUGS 35 同源的越权面）
3. record_turn 的累加语义：last_intent 覆盖、订单号只在确认归属时覆盖、
   切单时旧单留在 resolved_orders、不传订单号时保留旧单
4. 过期作用域：订单状态 30min 失效，买家自述事实（facts）不失效
"""
import datetime
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.context import context_service as cs
from app.services.context.context_service import ContextService


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return None if isinstance(self._value, tuple) else self._value

    def first(self):
        return self._value if isinstance(self._value, tuple) else None


class _FakeDB:
    """按调用顺序依次返回 preset 结果的假 session"""

    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self._results.pop(0))

    def add(self, row):
        self.added.append(row)


@contextmanager
def _fake_session(db):
    yield db


def _patch_db(monkeypatch, db):
    monkeypatch.setattr(cs, "with_safe_session", lambda commit=False: _fake_session(db))


def _conv(conv_id=7):
    return SimpleNamespace(id=conv_id)


def _row(user_id=42, order_no=None, resolved=None, idle_minutes=0, facts=None):
    return SimpleNamespace(
        user_id=user_id,
        last_intent="order_query",
        current_order_no=order_no,
        flow_state=None,
        resolved_orders=resolved,
        flow_payload=None,
        facts=facts,
        update_time=datetime.datetime.now() - datetime.timedelta(minutes=idle_minutes),
    )


def _hit(row, idle_minutes=0):
    """load 现在一次查出 (row, 空闲分钟数)——过期判断在 SQL 里，但读出来再用。"""
    return (row, idle_minutes)


@pytest.fixture(autouse=True)
def _store_on(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_CONTEXT_STORE", True)


class TestLoad:

    def test_switch_off_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLE_CONTEXT_STORE", False)
        ctx = ContextService().load("s1", 42)
        assert ctx.current_order_no is None

    def test_conversation_missing(self, monkeypatch):
        _patch_db(monkeypatch, _FakeDB([None]))
        ctx = ContextService().load("s1", 42)
        assert ctx.session_id == "s1" and ctx.current_order_no is None

    def test_row_missing(self, monkeypatch):
        _patch_db(monkeypatch, _FakeDB([_conv(), None]))
        assert ContextService().load("s1", 42).current_order_no is None

    def test_normal_hit(self, monkeypatch):
        _patch_db(monkeypatch, _FakeDB([
            _conv(),
            _hit(_row(order_no="ORD20260622003", resolved=["ORD20260622003"])),
        ]))
        ctx = ContextService().load("s1", 42)
        assert ctx.current_order_no == "ORD20260622003"
        assert ctx.resolved_orders == ["ORD20260622003"]

    def test_cross_user_rejected(self, monkeypatch):
        """行属于别人 → 返回空上下文，绝不把他人订单号交出去"""
        _patch_db(monkeypatch, _FakeDB([_conv(), _hit(_row(user_id=99, order_no="ORD20260622999"))]))
        ctx = ContextService().load("s1", 42)
        assert ctx.current_order_no is None
        assert ctx.facts == {}

    def test_stale_keeps_facts_drops_order(self, monkeypatch):
        """过期只裁订单状态，买家自述事实（身高/尺码）保留。

        订单号过期是因为「买家可能早换了单」，继续用会串单；
        身高体重不会因为 40 分钟没说话就变成假的。
        """
        db = _FakeDB([
            _conv(),
            _hit(_row(order_no="ORD20260622003", resolved=["ORD20260622003"],
                      facts={"size": {"value": "L"}}), idle_minutes=31),
        ])
        _patch_db(monkeypatch, db)
        ctx = ContextService().load("s1", 42)
        assert ctx.current_order_no is None
        assert ctx.resolved_orders == []
        assert ctx.facts["size"]["value"] == "L"
        # 防回归：空闲时长仍由 DB 时钟算（timestampdiff），不回到 Python 减法
        sql = " ".join(str(s) for s in db.statements).lower()
        assert "timestampdiff" in sql

    def test_fresh_context_kept(self, monkeypatch):
        _patch_db(monkeypatch, _FakeDB([_conv(), _hit(_row(order_no="ORD20260622003"), 29)]))
        assert ContextService().load("s1", 42).current_order_no == "ORD20260622003"


class TestRecordTurn:

    def test_switch_off_short_circuits(self, monkeypatch):
        monkeypatch.setattr(settings, "ENABLE_CONTEXT_STORE", False)
        assert ContextService().record_turn("s1", 42, last_intent="order_query") is True

    def test_conversation_missing_returns_false(self, monkeypatch):
        db = _FakeDB([None])
        _patch_db(monkeypatch, db)
        assert ContextService().record_turn("s1", 42, last_intent="order_query") is False
        assert db.added == []

    def test_insert_new_row(self, monkeypatch):
        db = _FakeDB([_conv(), None])
        _patch_db(monkeypatch, db)
        ok = ContextService().record_turn(
            "s1", 42, last_intent="refund_query",
            order_no="ORD20260622003", order_no_owned=True,
        )
        assert ok is True
        row = db.added[0]
        assert row.last_intent == "refund_query"
        assert row.current_order_no == "ORD20260622003"
        assert row.resolved_orders == ["ORD20260622003"]

    def test_unowned_order_not_written(self, monkeypatch):
        """订单不属于该用户 → 不写 current_order_no（写进去就会越权回答）"""
        db = _FakeDB([_conv(), None])
        _patch_db(monkeypatch, db)
        ContextService().record_turn(
            "s1", 42, last_intent="order_query",
            order_no="ORD20260622099", order_no_owned=False,
        )
        row = db.added[0]
        assert row.current_order_no is None
        assert row.resolved_orders == []

    def test_no_order_keeps_previous(self, monkeypatch):
        """第二轮只说"那能退吗"→ 上一轮锁定的订单不能被清空"""
        row = _row(order_no="ORD20260622003", resolved=["ORD20260622003"])
        _patch_db(monkeypatch, _FakeDB([_conv(), row]))
        ContextService().record_turn("s1", 42, last_intent="refund_query")
        assert row.current_order_no == "ORD20260622003"
        assert row.last_intent == "refund_query"

    def test_switch_order_overwrites_and_keeps_history(self, monkeypatch):
        row = _row(order_no="ORD20260622003", resolved=["ORD20260622003"])
        _patch_db(monkeypatch, _FakeDB([_conv(), row]))
        ContextService().record_turn(
            "s1", 42, last_intent="order_query",
            order_no="ORD20260701001", order_no_owned=True,
        )
        assert row.current_order_no == "ORD20260701001"
        assert row.resolved_orders == ["ORD20260701001", "ORD20260622003"]

    def test_cross_user_write_rejected(self, monkeypatch):
        row = _row(user_id=99, order_no="ORD20260622999")
        _patch_db(monkeypatch, _FakeDB([_conv(), row]))
        ok = ContextService().record_turn(
            "s1", 42, last_intent="order_query",
            order_no="ORD20260622003", order_no_owned=True,
        )
        assert ok is False
        assert row.current_order_no == "ORD20260622999"


class TestUpdateIsolation:

    def test_update_rejects_other_users_row(self, monkeypatch):
        row = _row(user_id=99)
        _patch_db(monkeypatch, _FakeDB([_conv(), row]))
        ok = ContextService().update("s1", 42, last_intent="refund_query")
        assert ok is False
        assert row.last_intent == "order_query"  # 未被覆盖

    def test_update_none_fields_not_overwritten(self, monkeypatch):
        row = _row(order_no="ORD20260622003")
        _patch_db(monkeypatch, _FakeDB([_conv(), row]))
        ContextService().update("s1", 42, last_intent="refund_query")
        assert row.current_order_no == "ORD20260622003"
        assert row.last_intent == "refund_query"


def test_no_db_call_when_switch_off(monkeypatch):
    """开关关闭时不许碰 DB（灰度回滚要能真回滚）"""
    monkeypatch.setattr(settings, "ENABLE_CONTEXT_STORE", False)

    def _boom(*_a, **_k):
        raise AssertionError("开关关闭时不应访问 DB")

    monkeypatch.setattr(cs, "with_safe_session", _boom)
    svc = ContextService()
    assert svc.load("s1", 42).current_order_no is None
    assert svc.record_turn("s1", 42, last_intent="order_query") is True
