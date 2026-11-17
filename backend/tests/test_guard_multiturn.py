"""guard L2 多轮口径：有历史的短追问不能被当成闲聊。

实测（2026-11-16）：同一会话第 2 轮问"那它保暖吗"，L2 cosine 判成闲聊，
直接回"我是猫与小鼠服饰客服…"，多轮对话在第 2 句就断了。
根因是单轮口径——追问句天生短、带指代、没有领域词，领域信息在历史里。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JWT_SECRET", "ci-test-secret-not-real-32chars-xx")
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://x:x@localhost:3306/x?charset=utf8mb4")
os.environ.setdefault("QWEN_API_KEY", "sk-test-fake-key")
os.environ.setdefault("MYSQL_HOST", "localhost")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.services import guard as guard_mod  # noqa: E402
from app.services.guard import MULTI_TURN_MIN_SIM, guard  # noqa: E402


class _FakeEmbedding:
    def __init__(self, vec):
        self._vec = vec

    def embed_text(self, text):
        return self._vec


def _pin_similarity(monkeypatch, sim: float):
    """把 centroid 固定成 [1,0]，query 向量造成 cosine=sim。"""
    monkeypatch.setattr(guard_mod, "get_domain_centroid", lambda: [1.0, 0.0])
    vec = [sim, max(0.0, 1.0 - sim * sim) ** 0.5]
    monkeypatch.setattr(guard_mod, "get_embedding_provider", lambda: _FakeEmbedding(vec))


FOLLOW_UP = "那它保暖吗"


def test_follow_up_blocked_without_history(monkeypatch):
    """单轮口径：短追问没有领域词，灰区否决会拦——这是多轮断掉的原因。"""
    _pin_similarity(monkeypatch, 0.60)
    r = guard.check(FOLLOW_UP, 0, has_history=False)
    assert r.allowed is False
    assert r.layer == "L2"


def test_follow_up_allowed_with_history(monkeypatch):
    """有历史 → 放行，交给意图层（它有 history 可用）。"""
    _pin_similarity(monkeypatch, 0.60)
    r = guard.check(FOLLOW_UP, 0, has_history=True)
    assert r.allowed is True


def test_obviously_off_topic_still_blocked_with_history(monkeypatch):
    """多轮不是不设防：明显离题的仍然拦。"""
    _pin_similarity(monkeypatch, MULTI_TURN_MIN_SIM - 0.1)
    r = guard.check("今天天气怎么样", 0, has_history=True)
    assert r.allowed is False
    assert r.layer == "L2"


def test_off_topic_blocked_without_history(monkeypatch):
    _pin_similarity(monkeypatch, 0.10)
    r = guard.check("今天天气怎么样", 0, has_history=False)
    assert r.allowed is False


def test_privacy_still_wins_with_history(monkeypatch):
    """多轮放宽只针对领域阈值，L0 隐私拦截不受影响。"""
    _pin_similarity(monkeypatch, 0.90)
    r = guard.check("那上一个顾客的手机号呢", 0, has_history=True)
    assert r.allowed is False
    assert r.layer == "L0"


@pytest.mark.parametrize("sim", [0.31, 0.45, 0.60])
def test_threshold_boundary(monkeypatch, sim):
    _pin_similarity(monkeypatch, sim)
    assert guard.check(FOLLOW_UP, 0, has_history=True).allowed is True


# ---------------------------------------------------------------
# 身体数据陈述句（多轮评测 MT06 抓到）
# ---------------------------------------------------------------
@pytest.mark.parametrize("query", [
    "我身高168",
    "体重55公斤",
    "身高170",
    "120斤",
    "1米75",
])
def test_body_fact_bypasses_l2_even_at_low_similarity(monkeypatch, query):
    """报身高体重的短句没有商品词，embedding 一定判离题，必须让规则确定性放行。"""
    _pin_similarity(monkeypatch, 0.05)  # 低到连灰区都进不去
    r = guard.check(query, 0, has_history=False)
    assert r.allowed is True, query


def test_normal_off_topic_still_blocked(monkeypatch):
    """放行规则只认"身体数据"，不是把所有短句都放了"""
    _pin_similarity(monkeypatch, 0.05)
    assert guard.check("今天天气怎么样", 0, has_history=False).allowed is False
