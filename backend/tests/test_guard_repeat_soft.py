"""guard L3 双阈值单测（防刷从"一超就拦"改成"软限降级 + 硬限拦截"）

背景：原规则 1min 内同一句 >3 次直接拦。但买家重复问，绝大多数是上一轮没解决，
拦掉等于把最需要帮助的人挡在门外，而且拦截不落库、不进质检，属于隐形投诉。

新语义：
- count > 3  → 放行 + degraded（调用方跳缓存 + 进 badcase）
- count > 10 → 硬拦（真刷子）
"""
import pytest

from app.services import guard as guard_module
from app.services.guard import InputGuard


class _FakeRedis:
    def __init__(self, count):
        self.count = count
        self.expired = []

    def incr(self, _key):
        return self.count

    def expire(self, key, ttl):
        self.expired.append((key, ttl))


def _patched(monkeypatch, count):
    fake = _FakeRedis(count)
    monkeypatch.setattr(guard_module, "redis_get", lambda: fake)
    return fake


class TestRepeatSoftLimit:

    def test_below_soft_limit_passes(self, monkeypatch):
        _patched(monkeypatch, 3)
        assert InputGuard()._check_l3(42, "退货运费谁出") is None

    def test_soft_limit_degrades_not_blocks(self, monkeypatch):
        """第 4 次重复：放行，但标记 degraded"""
        _patched(monkeypatch, 4)
        r = InputGuard()._check_l3(42, "退货运费谁出")
        assert r is not None
        assert r.allowed is True
        assert r.degraded is True
        assert r.reason == "repeat_soft"
        assert r.response is None  # 不放话术：用户要的是答案，不是"你重复了"

    def test_hard_limit_blocks(self, monkeypatch):
        _patched(monkeypatch, 11)
        r = InputGuard()._check_l3(42, "退货运费谁出")
        assert r is not None
        assert r.allowed is False
        assert r.reason == "spam"
        assert r.response

    def test_anonymous_skipped(self, monkeypatch):
        """匿名用户不计数（也没法稳定计数）"""
        fake = _patched(monkeypatch, 99)
        assert InputGuard()._check_l3(0, "在吗") is None
        assert fake.expired == []

    def test_redis_error_passes(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("redis down")

        monkeypatch.setattr(guard_module, "redis_get", _boom)
        assert InputGuard()._check_l3(42, "在吗") is None

    def test_expire_set_on_first_hit(self, monkeypatch):
        fake = _patched(monkeypatch, 1)
        assert InputGuard()._check_l3(42, "在吗") is None
        assert len(fake.expired) == 1


class TestCheckReturnsDegraded:

    def test_check_passes_degraded_through(self, monkeypatch):
        """check() 不许把降级结果吞掉，也不许记成拦截"""
        _patched(monkeypatch, 5)
        g = InputGuard()
        # 绕开 L0/L1/L2：只验证 L3 的降级能透传
        monkeypatch.setattr(g, "_check_other_private", lambda _q: None)
        monkeypatch.setattr(g, "_check_l1", lambda _q: None)
        monkeypatch.setattr(g, "_check_l2", lambda _q, has_history=False: None)
        recorded = []
        monkeypatch.setattr(g, "_record_block", staticmethod(lambda r: recorded.append(r)))
        r = g.check("退货运费谁出", 42)
        assert r.allowed is True and r.degraded is True
        assert recorded == []  # 降级不是拦截，不能进拦截统计
