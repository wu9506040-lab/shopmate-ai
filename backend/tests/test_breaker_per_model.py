"""
断路器按模型分桶（BUGS 61）

实测事故：意图模型 qwen-turbo 免费额度撞 429，连续失败把**共用**断路器
打到 OPEN，结果主力模型 qwen-plus 明明可用也被全部短路。配额是「账号 ×
模型」维度的，断路器也该是这个粒度。

覆盖：
1. 同一模型拿到同一桶；不同模型各自一桶
2. 一个桶 OPEN 不影响另一个桶（核心）
3. chat(model=A) 限流后，chat(model=B) 仍能成功
4. get_breaker_stats 能按模型导出
5. reset_breaker(model) 只重置指定桶
"""
from unittest.mock import MagicMock, patch

import pytest
from openai import APIConnectionError

from app.core import qwen
from app.core.circuit_breaker import CircuitOpenError


def _ok_response(text: str = "ok"):
    r = MagicMock()
    r.choices = [MagicMock(message=MagicMock(content=text, tool_calls=None))]
    r.usage.prompt_tokens = 1
    r.usage.completion_tokens = 1
    r.usage.total_tokens = 2
    return r


def test_same_model_same_bucket():
    qwen.reset_breaker()
    assert qwen._breaker_for("qw-a") is qwen._breaker_for("qw-a")


def test_different_models_get_different_buckets():
    qwen.reset_breaker()
    a, b = qwen._breaker_for("qw-a"), qwen._breaker_for("qw-b")
    assert a is not b
    assert a.name != b.name


def test_open_bucket_does_not_affect_other_model():
    """核心：一个模型限流，不能把另一个模型一起停掉。"""
    qwen.reset_breaker()
    sick, healthy = qwen._breaker_for("qw-sick"), qwen._breaker_for("qw-healthy")

    def boom():
        raise APIConnectionError(request=MagicMock())

    sick.failure_threshold = 2
    for _ in range(2):
        with pytest.raises(APIConnectionError):
            sick.call(boom)
    with pytest.raises(CircuitOpenError):
        sick.call(boom)

    # 同一时刻，健康的桶照常用
    assert healthy.call(lambda: "ok") == "ok"


def test_chat_rate_limited_model_does_not_short_circuit_main_model():
    """端到端复现事故：廉价模型 429 后，主力模型仍能出答案。"""
    qwen.reset_breaker()
    tiny, main = "qw-tiny-probe", qwen.QWEN_MODEL
    qwen._breaker_for(tiny).failure_threshold = 2

    def create(**kw):
        if kw["model"] == tiny:
            raise APIConnectionError(request=MagicMock())
        return _ok_response()

    client = MagicMock()
    client.chat.completions.create = create

    with patch.object(qwen, "get_client", return_value=client), \
            patch.object(qwen.time, "sleep"):
        for _ in range(2):
            with pytest.raises((APIConnectionError, CircuitOpenError)):
                qwen.chat([{"role": "user", "content": "hi"}], model=tiny)
        with pytest.raises(CircuitOpenError):
            qwen.chat([{"role": "user", "content": "hi"}], model=tiny)

        # 主力模型不受牵连——修复前这里会拿到 CircuitOpenError
        out = qwen.chat([{"role": "user", "content": "hi"}], model=main)

    assert out["reply"] == "ok"


def test_stats_include_per_model_buckets():
    qwen.reset_breaker()
    qwen._breaker_for("qw-stats-probe")
    stats = qwen.get_breaker_stats()

    assert stats["default"]["model"] == qwen.QWEN_MODEL
    assert "qw-stats-probe" in stats["models"]


def test_reset_single_model_keeps_others_open():
    """只重置一个桶，别的桶该开着还开着（否则会把未恢复的故障也放过去）。"""
    qwen.reset_breaker()
    a, b = qwen._breaker_for("qw-reset-a"), qwen._breaker_for("qw-reset-b")

    def boom():
        raise APIConnectionError(request=MagicMock())

    for br in (a, b):
        br.failure_threshold = 1
        with pytest.raises(APIConnectionError):
            br.call(boom)

    qwen.reset_breaker("qw-reset-a")

    assert a.call(lambda: "ok") == "ok"          # 被重置，能通
    with pytest.raises(CircuitOpenError):
        b.call(lambda: "ok")                      # 没被重置，仍开路


def test_reset_all_models():
    qwen.reset_breaker()
    b = qwen._breaker_for("qw-reset-all")

    def boom():
        raise APIConnectionError(request=MagicMock())

    b.failure_threshold = 1
    with pytest.raises(APIConnectionError):
        b.call(boom)
    with pytest.raises(CircuitOpenError):
        b.call(lambda: "ok")

    qwen.reset_breaker()
    assert b.call(lambda: "ok") == "ok"
