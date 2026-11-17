"""agent_runner 循环治理的接线测试。

单测只证明 LoopGuard 判得对，这里证明**它在循环里真的被调用了**：
顺序错一步（比如 record 放在 dispatch 前）就会出现"第一次调用就被拦"，
或者写操作照样执行两次——这两种都不是纯函数能测出来的。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.chat import agent_runner


class _FakeLLM:
    """按脚本依次返回；记录每次请求的 messages 快照和 tool_choice。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        if not self._script:
            return {"reply": "（脚本用尽）", "tool_calls": []}
        return self._script.pop(0)


def _tool_call(name: str, args: str, call_id: str = "c1") -> dict:
    return {
        "reply": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": args},
        }],
    }


def _wire(monkeypatch, script, dispatched: list):
    fake = _FakeLLM(script)

    def _dispatch(name, args_raw, ctx):
        dispatched.append((name, args_raw))
        return {"items": [{"sku": "SKU1", "name": "针织衫"}], "total": 1}

    monkeypatch.setattr(agent_runner, "settings",
                        SimpleNamespace(ENABLE_AGENT_FC=True, MAX_AGENT_TURNS=5))
    monkeypatch.setattr(agent_runner, "get_llm_provider", lambda: fake)
    monkeypatch.setattr(agent_runner, "dispatch", _dispatch)
    monkeypatch.setattr(agent_runner, "_load_system_prompt", lambda: "你是客服")
    return fake


def test_repeated_read_call_blocked_then_forced_final(monkeypatch):
    """同参只读工具：执行 2 次后拦截；连续两轮全重复 → 禁工具收口。"""
    dispatched: list = []
    args = '{"query": "针织衫"}'
    script = [
        _tool_call("search_product", args, "c1"),
        _tool_call("search_product", args, "c2"),
        _tool_call("search_product", args, "c3"),
        _tool_call("search_product", args, "c4"),
        {"reply": "这款针织衫是羊毛混纺，售价 199。", "tool_calls": []},
    ]
    fake = _wire(monkeypatch, script, dispatched)

    events = list(agent_runner.run_stream_agent("针织衫多少钱", user_id=1))

    assert len(dispatched) == 2                       # 第 3、4 次不再执行
    skipped = [e[1] for e in events if e[0] == "meta" and "tool_skipped" in e[1]]
    assert len(skipped) == 2
    assert all(s["tool_skipped"]["reason"] == "repeat_read" for s in skipped)

    forced = [e[1] for e in events if e[0] == "meta" and e[1].get("forced_final")]
    assert forced and forced[0]["loop_guard"]["skipped"] == 2
    assert fake.calls[-1]["tool_choice"] == "none"     # 收敛时禁工具
    assert events[-1][0] == "done"
    assert "羊毛混纺" in events[-1][1]["answer"]


def test_write_tool_double_call_blocked(monkeypatch):
    """写操作同参第二次必须拦：这是二次改地址/二次发货的防线。"""
    dispatched: list = []
    args = '{"order_no": "ORD001", "new_address": "北京市朝阳区", "confirmed": true}'
    script = [
        _tool_call("modify_address", args, "c1"),
        _tool_call("modify_address", args, "c2"),
        {"reply": "地址已修改。", "tool_calls": []},
    ]
    _wire(monkeypatch, script, dispatched)

    events = list(agent_runner.run_stream_agent("改地址", user_id=1))

    assert len(dispatched) == 1
    skipped = [e[1]["tool_skipped"] for e in events if e[0] == "meta" and "tool_skipped" in e[1]]
    assert skipped[0]["reason"] == "repeat_write"


def test_skipped_tool_message_keeps_data(monkeypatch):
    """拦截后回灌的 tool 消息里必须还有原结果字段，否则模型只能编。"""
    dispatched: list = []
    args = '{"query": "针织衫"}'
    script = [
        _tool_call("search_product", args, "c1"),
        _tool_call("search_product", args, "c2"),
        _tool_call("search_product", args, "c3"),
        {"reply": "ok", "tool_calls": []},
    ]
    fake = _wire(monkeypatch, script, dispatched)

    list(agent_runner.run_stream_agent("针织衫", user_id=1))

    # 拦截发生在第 3 轮处理时，所以第 4 轮请求里才看得到那条回灌消息
    last_msgs = fake.calls[-1]["messages"]
    tail_tool = [m for m in last_msgs if m.get("role") == "tool"][-1]
    assert "SKU1" in tail_tool["content"]
    assert "_loop_guard" in tail_tool["content"]


def test_normal_single_call_unaffected(monkeypatch):
    """一次工具 + 一次作答的老路径不许被循环治理改坏。"""
    dispatched: list = []
    script = [
        _tool_call("lookup_order", '{"order_no": "ORD001"}', "c1"),
        {"reply": "已发货。", "tool_calls": []},
    ]
    fake = _wire(monkeypatch, script, dispatched)

    events = list(agent_runner.run_stream_agent("到哪了", user_id=1))

    assert len(dispatched) == 1
    assert not [e for e in events if e[0] == "meta" and "tool_skipped" in e[1]]
    assert fake.calls[1]["tool_choice"] == "auto"
    assert events[-1][1]["answer"] == "已发货。"
