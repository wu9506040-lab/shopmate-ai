"""流式 FC 改造的接线测试（2026-11-17）。

改造前：每轮非流式 chat()，最后一轮拿到整段回复后 `for ch in reply` 逐字吐
——纯装饰，TTFT = 全量生成时间。
改造后：provider.stream_chat_events 边生成边下发，tool_calls 在流末集成。

这里要证明的三件事（纯函数测不出来）：
1. token 事件真的在 done 之前就到了下游（不是攒完再发）
2. 流式下 tool_calls 依然能被拼出来并走 dispatch
3. 流式不可用/中途炸掉时，能退回非流式而不是把整条链路带崩
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core import qwen
from app.services.chat import agent_runner


# =============================================================
# 假 chunk 对象（模拟 OpenAI 兼容流）
# =============================================================
class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, content=None, tool_calls=None, usage=None):
        if content is None and not tool_calls:
            self.choices = []          # usage 帧：choices 为空
        else:
            self.choices = [_Choice(_Delta(content, tool_calls))]
        self.usage = usage


def _tc(index, id=None, name=None, args=None):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=args),
    )


def _patch_open_stream(monkeypatch, chunks):
    monkeypatch.setattr(qwen, "_open_stream", lambda *a, **kw: ("qwen-test", iter(chunks)))


# =============================================================
# 一、tool_call 碎片拼接（纯函数）
# =============================================================
def test_merge_concatenates_argument_fragments():
    """arguments 是碎着来的，必须按 index 拼回去，少一片参数就是坏的 JSON。"""
    acc: dict = {}
    qwen._merge_tool_call(acc, _tc(0, id="call_1", name="lookup_order", args='{"order_no"'))
    qwen._merge_tool_call(acc, _tc(0, args=': "ORD'))
    qwen._merge_tool_call(acc, _tc(0, args='001"}'))

    calls = qwen._finalize_tool_calls(acc)
    assert len(calls) == 1
    assert calls[0]["id"] == "call_1"
    assert calls[0]["function"]["name"] == "lookup_order"
    assert calls[0]["function"]["arguments"] == '{"order_no": "ORD001"}'


def test_merge_keeps_parallel_calls_separate():
    """一次返回两个工具调用时按 index 分开，不能糊成一条。"""
    acc: dict = {}
    qwen._merge_tool_call(acc, _tc(0, id="a", name="search_product", args="{}"))
    qwen._merge_tool_call(acc, _tc(1, id="b", name="search_policy", args="{}"))

    calls = qwen._finalize_tool_calls(acc)
    assert [c["function"]["name"] for c in calls] == ["search_product", "search_policy"]


def test_finalize_synthesizes_missing_id_and_args():
    """缺 id → 得补一个（空 tool_call_id 会让下一轮请求 400）；缺参数补 {}。"""
    acc: dict = {}
    qwen._merge_tool_call(acc, _tc(0, name="search_policy"))

    calls = qwen._finalize_tool_calls(acc)
    assert calls[0]["id"].startswith("call_stream_")
    assert calls[0]["function"]["arguments"] == "{}"


def test_finalize_drops_nameless_fragment():
    """只有半截参数没有函数名的残片丢掉——拼不出调用，硬塞会 dispatch 到空名。"""
    acc: dict = {}
    qwen._merge_tool_call(acc, _tc(0, args='{"x": 1}'))

    assert qwen._finalize_tool_calls(acc) == []


# =============================================================
# 二、stream_chat_events 事件流
# =============================================================
def test_stream_events_emits_text_before_tool_calls(monkeypatch):
    """模型先说话再调工具：文本事件先出，tool_calls 在流末一次给。"""
    _patch_open_stream(monkeypatch, [
        _Chunk(content="我先查一下"),
        _Chunk(tool_calls=[_tc(0, id="call_1", name="lookup_order", args='{"order_no"')]),
        _Chunk(tool_calls=[_tc(0, args=': "ORD001"}')]),
    ])

    events = list(qwen.stream_chat_events([{"role": "user", "content": "到哪了"}]))

    assert events[0] == {"type": "text", "text": "我先查一下"}
    assert events[-1]["type"] == "tool_calls"
    assert events[-1]["tool_calls"][0]["function"]["arguments"] == '{"order_no": "ORD001"}'


def test_stream_events_no_tool_calls_when_plain_answer(monkeypatch):
    """纯文本回答不该凭空冒出一个空 tool_calls 事件。"""
    _patch_open_stream(monkeypatch, [_Chunk(content="你好"), _Chunk(content="，在的")])

    events = list(qwen.stream_chat_events([{"role": "user", "content": "在吗"}]))

    assert [e["type"] for e in events] == ["text", "text"]
    assert "".join(e["text"] for e in events) == "你好，在的"


def test_stream_events_forwards_usage(monkeypatch):
    """usage 帧（choices 为空）要能透出去，否则 D5 成本统计在 FC 路径上断掉。"""
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    _patch_open_stream(monkeypatch, [_Chunk(content="好"), _Chunk(usage=usage)])

    box: dict = {}
    list(qwen.stream_chat_events(
        [{"role": "user", "content": "在吗"}], on_usage=box.update,
    ))

    assert box["total"] == 15


# =============================================================
# 三、agent_runner 真流式接线
# =============================================================
class _StreamLLM:
    """带 stream_chat_events 的假 provider：按脚本逐轮吐事件。"""

    def __init__(self, script):
        self._script = list(script)
        self.chat_calls: list[dict] = []
        self.stream_calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.chat_calls.append({"messages": [dict(m) for m in messages], **kwargs})
        return {"reply": "（非流式兜底）", "tool_calls": []}

    def stream_chat_events(self, messages, **kwargs):
        self.stream_calls.append({"messages": [dict(m) for m in messages], **kwargs})
        script = self._script.pop(0) if self._script else [{"type": "text", "text": "（脚本用尽）"}]
        if isinstance(script, Exception):
            raise script
        yield from script


def _tool_call_turn(text: str, name: str, args: str, call_id: str = "c1"):
    return [
        {"type": "text", "text": text},
        {"type": "tool_calls", "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": args},
        }]},
    ]


def _wire(monkeypatch, script, dispatched, stream_fc=True):
    fake = _StreamLLM(script)

    def _dispatch(name, args_raw, ctx):
        dispatched.append((name, args_raw))
        return {"items": [{"sku": "SKU1", "name": "针织衫"}], "total": 1}

    monkeypatch.setattr(agent_runner, "settings", SimpleNamespace(
        ENABLE_AGENT_FC=True, MAX_AGENT_TURNS=5, AGENT_STREAM_FC=stream_fc,
    ))
    monkeypatch.setattr(agent_runner, "get_llm_provider", lambda: fake)
    monkeypatch.setattr(agent_runner, "dispatch", _dispatch)
    monkeypatch.setattr(agent_runner, "_load_system_prompt", lambda: "你是客服")
    return fake


def test_tokens_stream_before_done(monkeypatch):
    """单轮直接作答：token 必须一条条出现在 done 之前（真流式的唯一证据）。"""
    fake = _wire(monkeypatch, [[
        {"type": "text", "text": "七天"},
        {"type": "text", "text": "无理由"},
        {"type": "text", "text": "退货。"},
    ]], [])

    events = list(agent_runner.run_stream_agent("能退吗", user_id=1))
    kinds = [e[0] for e in events]
    tokens = [e[1] for e in events if e[0] == "token"]

    assert tokens == ["七天", "无理由", "退货。"]
    assert kinds.index("done") > max(i for i, k in enumerate(kinds) if k == "token")
    assert events[-1] == ("done", {"answer": "七天无理由退货。"})
    # 走的是流式出口，没有退回非流式
    assert fake.stream_calls and not fake.chat_calls
    assert fake.stream_calls[0]["tool_choice"] == "auto"


def test_streaming_tool_turn_keeps_preamble_text(monkeypatch):
    """流式下 tool_calls 仍要走 dispatch；先说的那半句要留在 assistant 消息里。"""
    dispatched: list = []
    fake = _wire(monkeypatch, [
        _tool_call_turn("我查一下订单", "lookup_order", '{"order_no": "ORD001"}'),
        [{"type": "text", "text": "已发货，明天到。"}],
    ], dispatched)

    events = list(agent_runner.run_stream_agent("到哪了", user_id=1))

    assert len(dispatched) == 1
    assert events[-1][1]["answer"] == "已发货，明天到。"
    # 第二轮请求里 assistant 那条带着用户已经看到的那句话
    second = fake.stream_calls[1]["messages"]
    assistant = [m for m in second if m.get("role") == "assistant"]
    assert assistant[0]["content"] == "我查一下订单"
    assert assistant[0]["tool_calls"][0]["id"] == "c1"


def test_stream_error_before_any_token_falls_back_to_chat(monkeypatch):
    """端点不认 tools+stream（一个 token 都没发出去）→ 退回非流式，本轮重跑。"""
    fake = _wire(monkeypatch, [
        RuntimeError("stream+tools not supported"),
    ], [])

    events = list(agent_runner.run_stream_agent("能退吗", user_id=1))

    assert fake.chat_calls                      # 确实退回去了
    assert events[-1] == ("done", {"answer": "（非流式兜底）"})


def test_stream_flag_off_uses_chat(monkeypatch):
    """AGENT_STREAM_FC=False → 一个流式调用都不发，行为回到改造前。"""
    fake = _wire(monkeypatch, [], [], stream_fc=False)

    events = list(agent_runner.run_stream_agent("能退吗", user_id=1))

    assert not fake.stream_calls
    assert fake.chat_calls
    assert events[-1] == ("done", {"answer": "（非流式兜底）"})


def test_provider_without_stream_events_uses_chat(monkeypatch):
    """老 provider / 测试 mock 没有 stream_chat_events → 自动退回，不报错。"""
    class _NoStream:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return {"reply": "老路径作答", "tool_calls": []}

    fake = _NoStream()
    monkeypatch.setattr(agent_runner, "settings", SimpleNamespace(
        ENABLE_AGENT_FC=True, MAX_AGENT_TURNS=5, AGENT_STREAM_FC=True,
    ))
    monkeypatch.setattr(agent_runner, "get_llm_provider", lambda: fake)
    monkeypatch.setattr(agent_runner, "dispatch", lambda *a, **k: {})
    monkeypatch.setattr(agent_runner, "_load_system_prompt", lambda: "你是客服")

    events = list(agent_runner.run_stream_agent("能退吗", user_id=1))

    assert events[-1] == ("done", {"answer": "老路径作答"})
