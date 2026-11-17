"""Agent 循环检测：同参重复调用识别 + 死循环收敛。

## 为什么需要

`agent_runner` 的循环只有 `MAX_AGENT_TURNS=5` 这一道闸，它管的是"最多跑几轮"，
不管"这几轮有没有往前走"。实测会出现两种病：

1. **只读工具打转**：`search_product` 参数没变却连调三次，模型每轮都拿同一份
   结果重新"综合"，白烧 3 轮 token。`slim()` 的 `_truncated` 提示只能提示，
   模型不一定听。
2. **写操作重放**（更严重）：`modify_address` / `auto_delivery` /
   `xianyu_send_message` 这类有副作用的工具，模型在 `confirmed=true` 之后又把
   同样的调用发一遍，就是二次改地址、二次发货。prompt 里写"不要重复调用"
   属于"求模型配合"，不是机制。

## 两条策略

- **写操作**：同参第二次直接拦，永不执行（结果原样回灌，让它看到"已做过"）。
- **只读工具**：允许重复 `max_read_repeat` 次（含首次）。给一次冗余是因为
  模型偶尔确实需要重看结果（前一条被总量护栏 stub 掉了）；超过就是打转。

## 收敛

连续两轮"全部调用都是重复"→ 判定打转，调用方用 `tool_choice="none"`
强制模型基于已有信息作答（比硬返 fallback 文案好：用户拿到的是真答案，
而不是"抱歉遇到复杂度限制"）。

## 边界

- 纯内存、无 IO、不读 DB，便于单测。
- 不做语义去重（"北京" 和 "北京市" 算两次）：宁可漏判，不可错拦——
  错拦一次只读工具会让模型拿不到数据，答错比多花一毛钱严重。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# 有副作用的工具：同参重复执行会造成真实损失。
# 名单要与 tools/registry.py 的写操作描述保持一致（那边标了"写操作"）。
WRITE_TOOLS: tuple[str, ...] = (
    "auto_delivery",
    "modify_address",
    "modify_contact",
    "xianyu_send_message",
)

_NOTE_READ = (
    "该工具已用相同参数调用过 {times} 次，结果不会变化。请直接使用上面已有的结果作答，"
    "或换工具/换参数；不要再用同样参数调用本工具。"
)
_NOTE_WRITE = (
    "该写操作已用相同参数执行过一次，本次已拦截（重复提交会造成二次扣款/二次发货/重复发消息）。"
    "上面的结果就是执行结果；如确需再次操作，先向用户确认。"
)


@dataclass(frozen=True)
class LoopRules:
    """循环检测阈值。

    max_read_repeat=2：首次 + 一次重看。定 1 太紧（前面结果被总量护栏 stub 掉后
    模型重取一次是合理行为），定 3 就等于放任打转。
    max_dup_turns=2：连续两轮全重复才收敛。只判一轮会把"模型先重看一遍再作答"
    误判成死循环。
    """

    max_read_repeat: int = 2
    max_dup_turns: int = 2
    write_tools: tuple[str, ...] = WRITE_TOOLS


@dataclass
class Decision:
    """一次调用的裁决结果。"""

    run: bool
    reason: str = ""      # "" | "repeat_read" | "repeat_write"
    content: str = ""     # run=False 时回灌给模型的 tool 消息内容
    times: int = 0        # 该签名此前已执行次数


def _signature(name: str, args_raw: str) -> str:
    """工具调用签名：name + 规范化后的参数。

    必须按 key 排序后再序列化——模型两次生成的 JSON 键序经常不同
    （`{"a":1,"b":2}` vs `{"b":2,"a":1}`），不归一化就永远判不出重复。
    解析失败时退回原始字符串（宁可漏判重复，也不能因为脏参数抛异常）。
    """
    raw = (args_raw or "").strip() or "{}"
    try:
        obj = json.loads(raw)
        normalized = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError, ValueError):
        normalized = raw
    return f"{name}::{normalized}"


def _with_note(content: str, note: str) -> str:
    """把提示塞进已有结果（不重发数据，省 context）。

    结果是 JSON 时加 `_loop_guard` 字段；不是 JSON 就在前面加一行文本。
    两条路都保证模型还能读到原来的值——拦截不等于把数据弄丢。
    """
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return f"{note}\n{content}" if content else note
    if not isinstance(obj, dict):
        return f"{note}\n{content}"
    obj["_loop_guard"] = note
    return json.dumps(obj, ensure_ascii=False)


class LoopGuard:
    """单次请求内的调用记录器（每次 run_stream_agent 新建一个）。"""

    def __init__(self, rules: Optional[LoopRules] = None) -> None:
        self._rules = rules or LoopRules()
        # sig -> {"times": int, "content": str}
        self._seen: dict[str, dict] = {}
        self._dup_turns = 0
        self.skipped = 0   # 累计拦截次数（进 meta，便于观察）

    # ---- 裁决 ----
    def check(self, name: str, args_raw: str) -> Decision:
        """执行前调用：要不要真跑。"""
        sig = _signature(name, args_raw)
        hit = self._seen.get(sig)
        times = hit["times"] if hit else 0

        if hit is None:
            return Decision(run=True, times=0)

        if name in self._rules.write_tools:
            self.skipped += 1
            return Decision(
                run=False,
                reason="repeat_write",
                content=_with_note(hit["content"], _NOTE_WRITE),
                times=times,
            )

        if times >= self._rules.max_read_repeat:
            self.skipped += 1
            return Decision(
                run=False,
                reason="repeat_read",
                content=_with_note(hit["content"], _NOTE_READ.format(times=times)),
                times=times,
            )

        return Decision(run=True, times=times)

    def record(self, name: str, args_raw: str, content: str) -> None:
        """执行后调用：记住这份结果，供后续重复调用回灌。"""
        sig = _signature(name, args_raw)
        prev = self._seen.get(sig)
        self._seen[sig] = {
            "times": (prev["times"] if prev else 0) + 1,
            "content": content,
        }

    # ---- 轮次收敛 ----
    def end_turn(self, all_repeated: bool) -> None:
        """一轮结束：全重复就累加，有真执行就清零。"""
        self._dup_turns = self._dup_turns + 1 if all_repeated else 0

    def should_finalize(self) -> bool:
        """是否该强制收敛（调用方用 tool_choice="none" 收口）。"""
        if self._dup_turns >= self._rules.max_dup_turns:
            logger.warning(
                f"loop_guard 判定打转：连续 {self._dup_turns} 轮全为重复调用，"
                f"累计拦截 {self.skipped} 次",
                extra={"intent": "agent_loop_guard"},
            )
            return True
        return False

    def stats(self) -> dict:
        return {
            "skipped": self.skipped,
            "dup_turns": self._dup_turns,
            "distinct_calls": len(self._seen),
        }


def load_rules() -> LoopRules:
    """从 settings 读阈值；缺失/异常一律退回默认。

    放 settings 而不是 memory.yaml：这是循环控制参数，不属于"记忆预算"账本，
    和 MAX_AGENT_TURNS 是一家人。
    """
    try:
        from app.core.config import settings

        return LoopRules(
            max_read_repeat=int(getattr(settings, "AGENT_REPEAT_READ_LIMIT", 2) or 2),
            max_dup_turns=int(getattr(settings, "AGENT_MAX_DUP_TURNS", 2) or 2),
        )
    except Exception as e:  # pragma: no cover - 仅配置异常时触发
        logger.warning(f"loop_guard 规则加载失败，用默认值: {e}")
        return LoopRules()


__all__ = ["LoopGuard", "LoopRules", "Decision", "load_rules", "WRITE_TOOLS"]
