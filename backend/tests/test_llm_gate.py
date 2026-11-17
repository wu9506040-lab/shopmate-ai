"""LLM 并发闸测试（step 6，2026-11-17）。

背景：并发闸原来只装在确定性编排的 `stream_llm()` 里。FC agent 走
`stream_chat_events` / 兜底 `chat()`，完全绕过闸——50 并发打满时它是那个
漏水的口子，闸容量就不再是上限。

这里证明四件事：
1. 闸吐出去的东西一定能收回（正常返回 / 抛异常 / 客户端断连三种退出路径）
2. 排队等不到 → 抛 DeadlineExceededError 且记指标，不静默放行
3. FC 流式路径真的过闸（闸满时 LLM 一次都不该被调到）
4. FC 非流式兜底路径也过闸
"""
from __future__ import annotations

import contextlib

import pytest

from app.core import deadline as _deadline
from app.core.config import settings
from app.services.chat import agent_runner, stream_dispatcher
from app.services.metrics import metrics


class _CountingSem:
    """记账用的假信号量：数清楚 acquire/release 是否配对。"""

    def __init__(self, allow: bool = True):
        self.allow = allow
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, timeout=None):
        self.acquire_calls += 1
        return self.allow

    def release(self):
        self.release_calls += 1

    @property
    def balanced(self) -> bool:
        return self.acquire_calls == self.release_calls


def _install(monkeypatch, allow: bool = True) -> _CountingSem:
    sem = _CountingSem(allow=allow)
    monkeypatch.setattr(stream_dispatcher, "_LLM_SEMAPHORE", sem)
    return sem


# =============================================================
# 一、闸本身的进出平衡
# =============================================================
def test_gate_releases_on_normal_exit(monkeypatch):
    sem = _install(monkeypatch)

    with stream_dispatcher.llm_gate():
        pass

    assert sem.acquire_calls == 1
    assert sem.balanced


def test_gate_releases_on_exception(monkeypatch):
    """调用方炸了也必须还令牌，否则一次异常就永久少一路并发。"""
    sem = _install(monkeypatch)

    with pytest.raises(ValueError):
        with stream_dispatcher.llm_gate():
            raise ValueError("boom")

    assert sem.balanced


def test_gate_releases_when_generator_closed(monkeypatch):
    """客户端断连 = generator.close() → GeneratorExit 穿过 with，闸必须回收。

    这是最容易漏的一条：yield 在 with 里，如果只写 acquire/release 手动配对，
    断连时 release 永远执行不到，跑几十个用户就把闸饿死。
    """
    sem = _install(monkeypatch)

    def _producer():
        with stream_dispatcher.llm_gate():
            yield "first"
            yield "second"

    gen = _producer()
    assert next(gen) == "first"
    gen.close()

    assert sem.balanced


def test_gate_timeout_raises_and_counts_metric(monkeypatch):
    """排队等不到：快速失败 + 记 sem_wait_timeout，不许无限等。"""
    _install(monkeypatch, allow=False)
    metrics.sem_wait_timeout_total = 0

    with pytest.raises(_deadline.DeadlineExceededError) as ei:
        with stream_dispatcher.llm_gate():
            pass

    assert ei.value.stage == "llm.semaphore"
    assert metrics.sem_wait_timeout_total == 1


# =============================================================
# 二、FC 路径确实过闸
# =============================================================
class _FakeLLM:
    """最小可用 provider：chat 会记次数，stream 版本见子类。"""

    def __init__(self, stream: bool = False):
        self.chat_calls = 0
        self.stream_calls = 0
        self._stream = stream

    def chat(self, **kwargs):
        self.chat_calls += 1
        return {"reply": "好的", "tool_calls": None}


class _FakeStreamLLM(_FakeLLM):
    def __init__(self):
        super().__init__(stream=True)

    def stream_chat_events(self, **kwargs):
        self.stream_calls += 1
        yield {"type": "text", "text": "好的"}
        yield {"type": "text", "text": "，已为您查询"}


@contextlib.contextmanager
def _fc_on(monkeypatch, llm):
    monkeypatch.setattr(settings, "ENABLE_AGENT_FC", True)
    monkeypatch.setattr(agent_runner, "get_llm_provider", lambda: llm)
    yield


def test_fc_streaming_path_waits_on_gate(monkeypatch):
    """闸满时 FC 流式路径必须排队失败，而不是偷跑一次 LLM 调用。"""
    _install(monkeypatch, allow=False)
    llm = _FakeStreamLLM()
    metrics.sem_wait_timeout_total = 0

    with _fc_on(monkeypatch, llm):
        with pytest.raises(_deadline.DeadlineExceededError):
            list(agent_runner.run_stream_agent("退货运费谁出", user_id=1))

    assert llm.stream_calls == 0      # 一次都没打到上游
    assert metrics.sem_wait_timeout_total == 1


def test_fc_non_stream_fallback_waits_on_gate(monkeypatch):
    """provider 没有流式出口 → 退非流式，那次 chat() 同样必须过闸。"""
    _install(monkeypatch, allow=False)
    llm = _FakeLLM(stream=False)     # 无 stream_chat_events 属性

    with _fc_on(monkeypatch, llm):
        with pytest.raises(_deadline.DeadlineExceededError):
            list(agent_runner.run_stream_agent("退货运费谁出", user_id=1))

    assert llm.chat_calls == 0


def test_fc_streaming_releases_gate_after_answer(monkeypatch):
    """正常答完：一笔借一笔还，不能占着闸不放。"""
    sem = _install(monkeypatch)
    llm = _FakeStreamLLM()

    with _fc_on(monkeypatch, llm):
        events = list(agent_runner.run_stream_agent("退货运费谁出", user_id=1))

    assert any(kind == "token" for kind, _ in events)
    assert sem.acquire_calls >= 1
    assert sem.balanced


def test_fc_disconnect_releases_gate(monkeypatch):
    """用户中途关页面：generator.close() 后闸必须回收。"""
    sem = _install(monkeypatch)
    llm = _FakeStreamLLM()

    with _fc_on(monkeypatch, llm):
        gen = agent_runner.run_stream_agent("退货运费谁出", user_id=1)
        next(gen)          # 拿到第一个 token 就断
        gen.close()

    assert sem.balanced
