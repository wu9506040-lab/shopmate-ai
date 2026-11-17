"""工具结果预算：什么进 messages，什么只留在日志里。

## 为什么需要（2026-11-17 harness 复盘）

`agent_runner` 把 `dispatch()` 的返回**全量** `json.dumps` 后塞进 messages：

    result = dispatch(name, args, ctx)
    messages.append({"role": "tool", "content": json.dumps(result)})

而 `_truncate_for_meta` 那个 500 字截断只作用于 meta 事件（前端展示），
**模型看到的那份是全量的**。商品搜索 20 条、物流轨迹 30 个节点，
一次就能吃 6-10k 字符；FC 是要跑多轮的，结果一直累积。

这是 Claude Code 五层压缩里的**第一层**（先砍工具输出，再砍历史），
我们原来顺序反了：history 有 1800 字硬预算，工具结果一点没有。

## 三条规矩

1. **错误不裁**：`{"error": ...}` 短且必须完整，截了模型没法自我修正。
2. **业务字段永不删**：`ticket_id` / `confirmed` / `order_no` 这类删一个字段，
   模型就可能把"已确认"当成"未确认"，把写操作做两遍。列表可以少几条，
   单据字段不能少一个。
3. **裁了必须说**：不给 `_truncated` 提示，模型会以为只有 5 个商品，
   答"我们只有 5 款"；或者用同样参数反复重调，一直耗到 MAX_AGENT_TURNS。

## 边界

- 纯函数，不读 settings、不写日志、不依赖 ToolSpec——便于单测和离线跑批。
- 不改变 `dispatch()` 契约：slim 在 agent_runner 里调，registry 不动。
- 只处理 dict（dispatch 已保证）；非 dict 包一层，不抛。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 这些 key 出现即视为"结果本身就有问题"，整条不裁。
_ERROR_KEYS = ("error", "errors")

# 业务单据字段：删了会让模型误判状态，永不裁、永不丢。
_PROTECTED_KEYS = (
    "ticket_id",
    "confirmed",
    "status",
    "order_no",
    "order_id",
    "sku",
    "code",
    "need_confirm",
)

# 默认丢弃字段：调试/内部信息，模型用不上。
_DEFAULT_DROP_KEYS = ("debug", "raw", "_internal", "trace", "sql", "stack")

_TRUNCATE_HINT = (
    "结果已截断。如需更多请缩小查询范围（如指定订单号/品类/时间），"
    "不要用相同参数重复调用本工具。"
)

# 被总量护栏丢掉的历史工具结果占位。必须明确说"可以重新调"，
# 否则模型会以为数据本来就没有，凭记忆编一个答案。
_DROPPED_STUB = (
    '{"_dropped": true, "note": "这条工具结果已因上下文超预算被省略。'
    '如仍需该数据，请重新调用对应工具。"}'
)


@dataclass(frozen=True)
class BudgetRules:
    """裁剪参数。默认值对应单条工具结果 ≤2000 字符。

    soft_ratio 是「宁可超一点，也不丢整条数据」的容忍带：
    实测商品搜索 5 条 2074 字符，只超 74（3.7%）——为这 74 字符砍掉 2 个商品
    是净亏。超得超过 15% 才进入降级阶梯。
    """

    max_chars: int = 2000
    max_list_items: int = 5
    max_field_chars: int = 300
    drop_keys: tuple[str, ...] = _DEFAULT_DROP_KEYS
    soft_ratio: float = 1.15
    # 整个 FC 循环里所有 tool 消息的累计上限（单条管住之后，多轮仍会叠加）
    context_max_chars: int = 6000


def _is_protected(key: str) -> bool:
    return key in _PROTECTED_KEYS


def _has_error(result: dict) -> bool:
    return any(k in result for k in _ERROR_KEYS)


def _measure(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


@dataclass
class _Stats:
    tool: str = ""
    orig_chars: int = 0
    slim_chars: int = 0
    dropped_items: int = 0
    dropped_fields: int = 0
    truncated: bool = False
    fields: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tool": self.tool,
            "orig_chars": self.orig_chars,
            "slim_chars": self.slim_chars,
            "dropped_items": self.dropped_items,
            "dropped_fields": self.dropped_fields,
            "truncated": self.truncated,
            "fields": self.fields,
        }


def _shrink(
    obj: Any,
    rules: BudgetRules,
    stats: _Stats,
    *,
    path: str = "",
) -> Any:
    """递归裁剪：列表截条数、长字符串截字符、丢调试字段。"""
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if k in rules.drop_keys and not _is_protected(k):
                stats.dropped_fields += 1
                continue
            out[k] = _shrink(v, rules, stats, path=f"{path}.{k}" if path else k)
        return out

    if isinstance(obj, list):
        # 列表本身可以被裁（除非它就在保护字段上，那种情况值通常是标量）
        kept = obj[: rules.max_list_items]
        if len(obj) > len(kept):
            stats.dropped_items += len(obj) - len(kept)
            stats.truncated = True
            stats.fields.append(
                {"field": path or "items", "shown": len(kept), "total": len(obj)}
            )
        return [_shrink(v, rules, stats, path=path) for v in kept]

    if isinstance(obj, str) and len(obj) > rules.max_field_chars:
        stats.truncated = True
        return obj[: rules.max_field_chars] + "…"

    return obj


def _reset(stats: _Stats) -> None:
    """降级重算前清计数（每次尝试都是独立的一次裁剪）。"""
    stats.dropped_items = 0
    stats.dropped_fields = 0
    stats.fields = []
    stats.truncated = False


def _ladder(rules: BudgetRules) -> tuple[tuple[int, int], ...]:
    """降级阶梯：(列表条数, 字段上限)。

    顺序是"先截字段，再砍条目"——顺序反过来会出大事：
    5 个商品各 410 字、总共 2050，只超 50 字，砍到 2 条直接丢 3 个商品；
    而把 attributes 从 300 截到 150 就够了，5 条一个不少。
    整条记录消失，比字段变短严重得多。
    """
    half = max(120, rules.max_field_chars // 2)
    return (
        (rules.max_list_items, half),
        (3, half),
        (1, half),
        (1, 100),
    )


def _hard_cap(result: dict, rules: BudgetRules, stats: _Stats) -> dict:
    """降级到底还超长时的兜底：保业务字段，其余压成一段预览。

    预览是字符串（不是半截 JSON），保证外层 dict 仍然可解析——
    模型拿到的是"这有个大结果，但只给你开头"，不是语法错误。
    """
    protected = {k: v for k, v in result.items() if _is_protected(k)}
    preview = json.dumps(result, ensure_ascii=False)[: rules.max_chars]
    protected["_truncated"] = {"hard_cap": True, "hint": _TRUNCATE_HINT}
    protected["preview"] = preview
    stats.truncated = True
    stats.fields.append({"field": "*", "shown": len(preview), "total": stats.orig_chars})
    return protected


def slim(
    name: str,
    result: Any,
    rules: Optional[BudgetRules] = None,
) -> tuple[dict, dict]:
    """把工具结果压到预算内，返回 (给模型的结果, 统计)。

    Args:
        name: 工具名（只用于统计，不参与裁剪决策）
        result: dispatch 返回（约定是 dict）
        rules: 裁剪参数；None 用默认值

    Returns:
        (slim_result, stats)。stats 进 meta，用来统计"哪个工具最费 context"。
    """
    rules = rules or BudgetRules()
    if not isinstance(result, dict):
        result = {"result": result}

    stats = _Stats(tool=name, orig_chars=_measure(result))

    # 规矩 1：错误不裁
    if _has_error(result):
        stats.slim_chars = stats.orig_chars
        return result, stats.as_dict()

    _reset(stats)
    slimmed = _shrink(result, rules, stats)

    # 总长兜底：先看是否落在容忍带内（超一点不丢数据），否则按阶梯收紧
    limit = int(rules.max_chars * rules.soft_ratio)
    if _measure(slimmed) > limit:
        for items, field_chars in _ladder(rules):
            _reset(stats)
            slimmed = _shrink(
                result,
                BudgetRules(
                    max_chars=rules.max_chars,
                    max_list_items=items,
                    max_field_chars=field_chars,
                    drop_keys=rules.drop_keys,
                    soft_ratio=rules.soft_ratio,
                ),
                stats,
            )
            if _measure(slimmed) <= limit:
                break

    if _measure(slimmed) > limit:
        # 阶梯砍到 1 条还超容忍带（字段数极多的异常结构），兜底成预览
        slimmed = _hard_cap(result, rules, stats)

    # 只有「丢了条目」才加提示：模型看到少了两条会以为数据就这些。
    # 单纯字段变短不用加——尾部的 "…" 已经自解释了，而 hint 本身 80 字，
    # 加在只省几十字的结果上会让它比原来更长（实测 2074 → 2349）。
    if stats.dropped_items and "_truncated" not in slimmed:
        slimmed["_truncated"] = {"fields": stats.fields, "hint": _TRUNCATE_HINT}

    stats.slim_chars = _measure(slimmed)
    if stats.truncated:
        logger.info(
            f"[tool_budget] {name} 已裁剪: {stats.orig_chars} → {stats.slim_chars} 字符 "
            f"dropped_items={stats.dropped_items} dropped_fields={stats.dropped_fields}"
        )
    return slimmed, stats.as_dict()


def enforce_total_budget(
    messages: list[dict],
    rules: Optional[BudgetRules] = None,
    keep_recent: int = 2,
) -> tuple[list[dict], dict]:
    """把整个 FC 循环里 tool 消息的累计长度压到 `context_max_chars` 内。

    为什么单条预算不够：单条 2000 是"一次调用最多吃多少"，但循环跑 5 轮、
    每轮 2 个工具，就是 10 条 × 2000 = 2 万字符——比 history（1800）
    和资料位（1000×3）加起来还大一个量级，最终把两者一起挤掉。

    策略：**从最早的开始换成 stub**，最近 `keep_recent` 条原样保留。
    - 从最早的开始：刚拿到的新结果是这一轮要用来作答的，动它必然答错；
    - 留最近 2 条而不是 1 条：model 常"先查 A 再查 B 再综合"，只留 B 就丢了 A；
    - stub 里写"可重新调用"：不然模型会当成"查无此数据"去编答案。

    Args:
        messages: 完整 FC messages（含 system/user/assistant/tool）
        rules: 预算（只用 context_max_chars）
        keep_recent: 尾部保留的 tool 消息条数

    Returns:
        (新 messages, stats)。原 list 不改，只有被 stub 的那几条换成新 dict。
    """
    rules = rules or BudgetRules()
    limit = rules.context_max_chars
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    orig = sum(len(messages[i].get("content") or "") for i in tool_idx)
    if orig <= limit or len(tool_idx) <= keep_recent:
        return messages, {"orig_chars": orig, "slim_chars": orig, "dropped": 0}

    out = list(messages)
    total = orig
    dropped = 0
    for i in tool_idx[: len(tool_idx) - keep_recent]:
        if total <= limit:
            break
        cur = out[i].get("content") or ""
        # 已经 stub 过 / 比 stub 还短：换了只会更长，跳过
        if cur == _DROPPED_STUB or len(cur) <= len(_DROPPED_STUB):
            continue
        total -= len(cur) - len(_DROPPED_STUB)
        out[i] = {**out[i], "content": _DROPPED_STUB}
        dropped += 1

    stats = {"orig_chars": orig, "slim_chars": total, "dropped": dropped}
    if dropped:
        logger.info(
            f"[tool_budget] 上下文总量超预算：{orig} → {total} 字符，"
            f"省略 {dropped} 条历史工具结果（保留最近 {keep_recent} 条）"
        )
    return out, stats


def load_rules() -> BudgetRules:
    """从 memory.yaml 读预算（与 history 预算同一份账本）。

    session 内只读一次也无所谓——yaml 本来就不参与热更新。
    配置缺失/类型错都退回默认值：预算用默认口径，总比主链路挂掉好。
    """
    try:
        from app.services.config_loader import get_config_loader

        cfg = get_config_loader().load("memory") or {}
    except Exception as e:  # pragma: no cover - 仅在配置缺失/损坏时触发
        logger.warning(f"[tool_budget] memory.yaml 加载失败，用默认预算: {e}")
        return BudgetRules()

    drop_raw = cfg.get("TOOL_RESULT_DROP_KEYS")
    drop_keys = (
        tuple(s.strip() for s in str(drop_raw).split(",") if s.strip())
        if drop_raw else _DEFAULT_DROP_KEYS
    )
    return BudgetRules(
        max_chars=int(cfg.get("TOOL_RESULT_MAX_CHARS", 2000)),
        max_list_items=int(cfg.get("TOOL_RESULT_MAX_LIST_ITEMS", 5)),
        max_field_chars=int(cfg.get("TOOL_RESULT_MAX_FIELD_CHARS", 300)),
        drop_keys=drop_keys,
        context_max_chars=int(cfg.get("TOOL_CONTEXT_MAX_CHARS", 6000)),
    )


__all__ = ["BudgetRules", "slim", "enforce_total_budget", "load_rules"]
