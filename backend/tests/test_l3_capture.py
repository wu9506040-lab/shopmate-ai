"""L3 写入闸单测

两道闸必须都在：
- 流量闸：只有 kind=biz 的会话能学（评测脚本不能污染真人记忆）
- 来源闸：只提交明确的事实（L2 已抽出的尺码 + 买家明说的面料偏好）

售后结论在白名单里但没有自动抽取器——规则抽语义会出错，宁可不写。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")

from app.core.config import settings  # noqa: E402
from app.models.session_meta import KIND_BIZ, KIND_EVAL, KIND_SMOKE  # noqa: E402
from app.services import l3_capture as cap  # noqa: E402


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_LONG_TERM_MEMORY", True)


def _stub(monkeypatch, kind=KIND_BIZ):
    """挡住 DB，记录 offer 调用"""
    calls = []
    monkeypatch.setattr(cap, "get_session_kind", lambda _sid: kind)
    monkeypatch.setattr(cap.l3, "offer_fact", lambda *a, **k: (calls.append(a) or True))
    return calls


class TestFabricPref:

    @pytest.mark.parametrize("query,expected", [
        ("我不喜欢羊毛，穿着扎", "不要羊毛"),
        ("我喜欢纯棉的，透气", "喜欢纯棉"),
        ("偏好真丝", "喜欢真丝"),
        ("别买涤纶的", "不要涤纶"),
    ])
    def test_explicit_preference(self, query, expected):
        assert cap.extract_fabric_pref(query) == expected

    @pytest.mark.parametrize("query", [
        "这件什么面料",
        "170~180cm 穿什么码",
        "面料成分是什么",
        "",
        None,
    ])
    def test_not_a_preference(self, query):
        assert cap.extract_fabric_pref(query) is None


class TestCaptureGate:

    def test_eval_traffic_not_captured(self, monkeypatch):
        calls = _stub(monkeypatch, KIND_EVAL)
        result = cap.capture_turn(
            42, "s1", "我平时穿L码", {"size": {"value": "L"}}
        )
        assert result == {}
        assert calls == []

    def test_smoke_traffic_not_captured(self, monkeypatch):
        calls = _stub(monkeypatch, KIND_SMOKE)
        cap.capture_turn(42, "s1", "我喜欢纯棉")
        assert calls == []

    def test_size_from_l2_facts(self, monkeypatch):
        calls = _stub(monkeypatch)
        cap.capture_turn(
            42, "s1", "那这件呢", {"size": {"value": "L"}, "height_cm": {"value": 175}}
        )
        assert calls == [(42, "size", "L", "s1", KIND_BIZ)]
        # 身高体重不进 L3：会过期，也可能改口

    def test_fabric_from_query(self, monkeypatch):
        calls = _stub(monkeypatch)
        cap.capture_turn(42, "s1", "我不喜欢羊毛", {})
        assert calls == [(42, "fabric_pref", "不要羊毛", "s1", KIND_BIZ)]

    def test_no_after_sales_auto_extraction(self, monkeypatch):
        """规则抽售后结论会出错（"我想退货"≠"历史售后结论"），所以不抽"""
        calls = _stub(monkeypatch)
        cap.capture_turn(42, "s1", "我想退货，上次那单还没到", {})
        assert calls == []

    def test_anonymous_skipped(self, monkeypatch):
        calls = _stub(monkeypatch)
        assert cap.capture_turn(0, "s1", "我穿L", {"size": {"value": "L"}}) == {}
        assert calls == []

    def test_switch_off_skipped(self, monkeypatch):
        calls = _stub(monkeypatch)
        monkeypatch.setattr(settings, "ENABLE_LONG_TERM_MEMORY", False)
        assert cap.capture_turn(42, "s1", "我穿L", {"size": {"value": "L"}}) == {}
        assert calls == []

    def test_db_error_swallowed(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(cap, "get_session_kind", lambda _s: KIND_BIZ)
        monkeypatch.setattr(cap.l3, "offer_fact", _boom)
        assert cap.capture_turn(42, "s1", "我穿L", {"size": {"value": "L"}}) == {}
