"""L3 跨会话长期记忆单测（假 session，不连真库）

为什么不用真库：tests/conftest.py 会把 DATABASE_URL 设成占位 mysql host，
测试模块里的 dotenv 不覆盖已存在变量，真库测试在 full suite 里必然失败。
SQL（DATE_ADD / ON DUPLICATE KEY / expire_at > NOW()）的正确性由端到端评测
`scripts/eval_longterm.py` 验证。

这里锁的是**规则**：白名单、流量闸、跨会话确认、改口、tombstone、prompt 边界。
"""
import os
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")

from app.models.session_meta import KIND_BIZ, KIND_EVAL  # noqa: E402
from app.services import long_term_memory as l3  # noqa: E402


class _Result:
    def __init__(self, first=None, rows=None, rowcount=0):
        self._first = first
        self._rows = rows or []
        self.rowcount = rowcount

    def first(self):
        return self._first

    def fetchall(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._first


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return self._results.pop(0) if self._results else _Result()


@contextmanager
def _fake_session(db):
    yield db


def _patch(monkeypatch, *results):
    db = _FakeDB(results)
    monkeypatch.setattr(l3, "with_safe_session", lambda commit=False: _fake_session(db))
    return db


def _sql(db, needle) -> bool:
    return any(needle in stmt for stmt, _ in db.calls)


class TestWhitelist:

    def test_non_whitelist_key_rejected_no_db(self, monkeypatch):
        db = _patch(monkeypatch)
        assert l3.offer_fact(42, "phone", "13800000000", "s1", KIND_BIZ) is False
        assert l3.offer_fact(42, "order_no", "ORD20260622003", "s1", KIND_BIZ) is False
        assert db.calls == []  # 白名单在碰 DB 之前就拦住

    def test_eval_traffic_never_written(self, monkeypatch):
        db = _patch(monkeypatch)
        assert l3.offer_fact(42, "size", "XXL", "s1", KIND_EVAL) is False
        assert db.calls == []

    def test_empty_value_rejected(self, monkeypatch):
        db = _patch(monkeypatch)
        assert l3.offer_fact(42, "size", "  ", "s1", KIND_BIZ) is False
        assert db.calls == []


class TestConfirmation:

    def test_first_session_creates_inactive_candidate(self, monkeypatch):
        db = _patch(monkeypatch, _Result(first=None), _Result())
        assert l3.offer_fact(42, "size", "L", "s1", KIND_BIZ) is True
        insert = db.calls[-1]
        assert "INSERT INTO" in insert[0]
        # 候选 active=0，不是一次会话就生效（VALUES 里第 6 位是 0）
        assert "hits, active, expire_at" in insert[0]
        assert "1, 0, DATE_ADD" in insert[0]

    def test_same_session_repeat_only_renews(self, monkeypatch):
        # _tombstoned 返 None；SELECT 返已有行
        db = _patch(
            monkeypatch,
            _Result(first=None),
            _Result(first=(1, "L", "s1", 1, 0)),
            _Result(),
        )
        assert l3.offer_fact(42, "size", "L", "s1", KIND_BIZ) is False
        assert _sql(db, "SET expire_at=DATE_ADD")

    def test_second_session_activates(self, monkeypatch):
        db = _patch(
            monkeypatch,
            _Result(first=None),
            _Result(first=(1, "L", "s1", 1, 0)),
            _Result(),
        )
        assert l3.offer_fact(42, "size", "L", "s2", KIND_BIZ) is True
        assert "active=:a" in db.calls[-1][0]
        assert db.calls[-1][1]["h"] == 2
        assert db.calls[-1][1]["a"] == 1

    def test_same_session_correction_not_treated_as_repeat(self, monkeypatch):
        """同会话内改口也必须更新——不能先判"同会话"就直接续期返回（BUGS 50）"""
        db = _patch(
            monkeypatch,
            _Result(first=None),
            _Result(first=(1, "L", "s1", 1, 0)),
            _Result(),
        )
        assert l3.offer_fact(42, "size", "XL", "s1", KIND_BIZ) is True
        assert "SET fact_value" in db.calls[-1][0]

    def test_correction_overwrites_immediately(self, monkeypatch):
        db = _patch(
            monkeypatch,
            _Result(first=None),
            _Result(first=(1, "L", "s1", 2, 1)),
            _Result(),
        )
        assert l3.offer_fact(42, "size", "XL", "s3", KIND_BIZ) is True
        params = db.calls[-1][1]
        assert params["v"] == "XL"
        assert params["p"] == "L"  # prev 留旧值
        assert "hits=1" in db.calls[-1][0]  # 改口后重新计数

    def test_read_filters_expiry_and_whitelist(self, monkeypatch):
        db = _patch(monkeypatch, _Result(rows=[
            ("size", "L"), ("fabric_pref", "不要羊毛"), ("phone", "13800000000"),
        ]))
        facts = l3.get_active_facts(42)
        assert facts == {"size": "L", "fabric_pref": "不要羊毛"}
        assert _sql(db, "expire_at > NOW()")
        assert _sql(db, "active=1")

    def test_read_empty_user(self, monkeypatch):
        db = _patch(monkeypatch)
        assert l3.get_active_facts(0) == {}
        assert db.calls == []


class TestDeletion:

    def test_single_key_soft_delete(self, monkeypatch):
        db = _patch(monkeypatch, _Result(rowcount=1))
        assert l3.delete_fact(42, "size") == 1
        assert "SET deleted=1" in db.calls[-1][0]
        assert db.calls[-1][1]["k"] == "size"

    def test_delete_all_writes_tombstone_first(self, monkeypatch):
        db = _patch(monkeypatch, _Result(rowcount=1), _Result(rowcount=2))
        assert l3.delete_fact(42) == 2
        first, second = db.calls
        assert "INSERT INTO" in first[0] and "__all__" in str(first[1])
        assert "SET deleted=1" in second[0]
        # 墓碑必须先写，否则并发写入能在中间插一行

    def test_tombstone_blocks_future_write(self, monkeypatch):
        db = _patch(monkeypatch, _Result(first=1))
        assert l3.offer_fact(42, "size", "L", "s3", KIND_BIZ) is False
        assert len(db.calls) == 1  # 查到墓碑就返回，不再 INSERT

    def test_delete_rejects_non_whitelist(self, monkeypatch):
        db = _patch(monkeypatch)
        assert l3.delete_fact(42, "phone") == 0
        assert db.calls == []


class TestPromptBlock:

    def test_only_whitelist_rendered(self):
        block = l3.to_prompt_block({
            "size": "L", "fabric_pref": "不要羊毛",
            "order_no": "ORD20260622003", "phone": "13800000000",
        })
        assert "常用尺码：L" in block
        assert "面料偏好：不要羊毛" in block
        assert "ORD" not in block and "138" not in block
        assert "本轮为准" in block

    def test_max_len(self):
        assert len(l3.to_prompt_block({"size": "x" * 500})) <= l3.MAX_L3_PROMPT_LEN

    def test_empty(self):
        assert l3.to_prompt_block({}) == ""
        assert l3.to_prompt_block(None) == ""
