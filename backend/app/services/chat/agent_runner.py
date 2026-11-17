"""
Agent FC Runner（C2）— Function Calling 编排核心

按 CLAUDE.md §9.2.2 模块隔离：
    - 本模块只做循环调度，不实现工具
    - 所有工具调用经 app.tools.registry.dispatch()（禁止直接 import 具体 Tool 类）

按 §9.7 自检 #1：禁止"模块 A 直接 import 模块 B 的具体类"——
    runner 不直接 import OrderTool/ProductTool/PolicyService；
    通过 ToolContext + dispatch() 抽象隔离。

主循环（≤ MAX_AGENT_TURNS）：
    messages = [system, *history, user]
    for turn in 1..MAX_AGENT_TURNS:
        messages = enforce_total_budget(messages)     # 累计 tool 消息压回预算
        text, tool_calls = yield from _turn_stream(...)   # 流式 FC：文本边生成边下发
        if tool_calls:
            append assistant tool_calls msg（content=已下发的 text）
            for each tc:
                if loop_guard.check(...) 判定为重复 → 回灌上次结果，不执行
                result = dispatch(name, args, ctx)
                append slim(result) tool msg                 # 单条预算
            if 连续多轮全重复 → tool_choice="none" 强制收口（同样流式）
            continue    # 下一轮 LLM 综合
        else:
            # 答复已经在流里发给前端了，这里只补 meta + done
            yield done
            return

    # 超限：返 fallback 文案

流式（2026-11-17）：
    改造前每轮用非流式 chat()，最后一轮拿到整段回复后再 for ch in reply 逐字吐
    ——纯装饰，TTFT 等于全量生成时间。现在走 provider.stream_chat_events：
    文本增量随生成下发，tool_calls 在流末一次集成。provider 不支持（测试 mock /
    老实现）或 AGENT_STREAM_FC=False 时自动退回非流式，行为与旧版一致。

异常策略：
    - ENABLE_AGENT_FC=False → 抛 RuntimeError（orchestrator 顶层 fallback 处理）
    - LLM 调用异常 / dispatch 异常 → 抛 RuntimeError（orchestrator 顶层 fallback 处理）
    - generator 内不 try/except（避免 yield 在 finally 内的 PEP 380 坑）
"""
import json
import logging
from typing import Any, Generator, Optional, Tuple

from app.core import deadline as _deadline
from app.core.config import settings
from app.core.providers.llm import get_llm_provider
from app.services.chat.loop_guard import LoopGuard, load_rules as load_loop_rules
from app.services.chat.stream_dispatcher import llm_gate
from app.services.prompt_loader import get_prompt_loader
from app.tools.registry import ToolContext, dispatch, to_openai_tools
from app.tools.result_budget import enforce_total_budget, load_rules, slim

logger = logging.getLogger(__name__)


# =============================================================
# 内部辅助
# =============================================================
def _load_system_prompt() -> str:
    """加载 FC agent system prompt（agent_fc.yaml）。

    失败时 fallback 到硬编码最小可用 prompt（避免 prompt_loader 故障阻断 Agent）。
    """
    try:
        return get_prompt_loader().load("agent_fc")
    except Exception as e:
        logger.warning(
            f"加载 agent_fc prompt 失败，使用硬编码 fallback: {e}",
            extra={"intent": "agent_fc"},
        )
        return (
            "你是「猫与小鼠」服饰客服中台的智能客服，可以使用 lookup_order / search_product / "
            "search_policy 三个工具。尺码数值、退换条款与运费规则必须引用检索结果，回答控制在 200 字以内。"
        )


def _format_history(history: Optional[list[dict]]) -> tuple[list[dict], str]:
    """history → (OpenAI messages, 较早对话要点)。

    Why 拆出 summary：`prepare_history_safe` 把被裁轮次压成 role="summary"
    的一条，而 OpenAI FC 只认 user/assistant/system/tool——原实现把它当陌生人
    直接丢掉，等于 FC 路径永远拿不到"较早对话要点"（RAG 路径有，FC 没有）。
    这里把 summary 摘出来交给调用方拼进 system，而不是塞成半路 system 消息
    （多段 system 的行为各家模型不一致）。

    其余 role 仅保留 user/assistant/system；历史里的 tool_calls 丢弃——
    OpenAI FC 要求完整 tool_call_id 链路，半截带着会让请求直接 400。
    """
    if not history:
        return [], ""
    msgs: list[dict] = []
    summaries: list[str] = []
    for h in history:
        role = h.get("role")
        content = h.get("content", "")
        if role == "summary":
            if content:
                summaries.append(content)
            continue
        if role in ("user", "assistant", "system") and content:
            msgs.append({"role": role, "content": content})
    return msgs, "；".join(summaries)


def _count_tool_calls(messages: list[dict]) -> int:
    """统计本会话累计工具调用次数（meta 暴露用）。"""
    return sum(
        1 for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    )


def _truncate_for_meta(d: dict, max_len: int = 500) -> dict:
    """meta 事件中 tool_result 截断（防 log 爆炸）。"""
    try:
        s = json.dumps(d, ensure_ascii=False)
        if len(s) > max_len:
            return {"_truncated": True, "_preview": s[:max_len]}
        return d
    except Exception:
        return {"_unserializable": True}


def _stream_events_of(llm):
    """取 provider 的流式 FC 出口；关了开关或 provider 没实现就返回 None。

    测试用的 fake provider 只有 chat()——这里返回 None 后自动走非流式，
    既有测试不用改一行。
    """
    if not getattr(settings, "AGENT_STREAM_FC", True):
        return None
    return getattr(llm, "stream_chat_events", None)


def _turn_stream(
    llm,
    messages: list[dict],
    openai_tools: list[dict],
    tool_choice: str,
    temperature: float,
    turn: int,
) -> Generator[Tuple[str, Any], None, None]:
    """跑一轮 LLM：文本边生成边 yield 给下游，同时收集 tool_calls。

    返回 (text, tool_calls) —— 用 `text, calls = yield from _turn_stream(...)`
    接返回值，既能转发 token 事件又能拿到这一轮的结论。

    text 的意义：模型可能先说了半句"我帮您查一下"再决定调工具。这段已经发给
    用户了，必须原样塞回 assistant 消息里（否则下一轮模型觉得自己没说过话，
    容易重复开场白）。

    降级两级：
    1. provider 没有 stream_chat_events / AGENT_STREAM_FC=False → 非流式 chat()
    2. 流式一个 token 都没发出去就报错（端点不认 tools+stream 组合很常见）
       → 同样退回非流式 chat()，这一轮重跑
    已经发出去字之后再报错就不救了（发出去的字收不回），抛给 orchestrator
    按"残答"处理——那条路径会补一句"刚才的回复中断了"。
    """
    stream_events = _stream_events_of(llm)

    if stream_events is None:
        with llm_gate():
            resp = llm.chat(
                messages=messages,
                tools=openai_tools,
                tool_choice=tool_choice,
                temperature=temperature,
            )
        text = resp.get("reply") or ""
        if text:
            yield ("token", text)
        return text, resp.get("tool_calls")

    text = ""
    tool_calls = None
    emitted = 0
    try:
        with llm_gate():
            for ev in stream_events(
                messages=messages,
                tools=openai_tools,
                tool_choice=tool_choice,
                temperature=temperature,
            ):
                etype = ev.get("type")
                if etype == "text":
                    piece = ev.get("text") or ""
                    if not piece:
                        continue
                    text += piece
                    emitted += 1
                    yield ("token", piece)
                elif etype == "tool_calls":
                    tool_calls = ev.get("tool_calls")
    except _deadline.DeadlineExceededError:
        # 排队就超预算：不是"端点不支持流式"，退非流式只是再排一次队、
        # 再吃一遍排队时长，等于把用户多挂 15 秒。直接上抛走降级。
        raise
    except Exception as e:  # noqa: BLE001
        if emitted:
            raise
        logger.warning(
            f"agent_runner turn={turn} 流式 FC 失败，退回非流式: "
            f"{type(e).__name__}: {str(e)[:120]}",
            extra={"intent": "agent_fc", "turn": turn, "stream_fallback": True},
        )
        with llm_gate():
            resp = llm.chat(
                messages=messages,
                tools=openai_tools,
                tool_choice=tool_choice,
                temperature=temperature,
            )
        text = resp.get("reply") or ""
        if text:
            yield ("token", text)
        return text, resp.get("tool_calls")

    if not text and not tool_calls:
        # 流跑完了一句没说、也没调工具：这不是"模型答空话"，是这一轮废了
        # （provider 声称支持流式 FC 实际没实现，最典型）。空手收场等于给
        # 用户一个空气泡，退回非流式重试一次，最多多花一次调用。
        logger.warning(
            f"agent_runner turn={turn} 流式 FC 空返回，退回非流式重试",
            extra={"intent": "agent_fc", "turn": turn, "stream_fallback": True},
        )
        with llm_gate():
            resp = llm.chat(
                messages=messages,
                tools=openai_tools,
                tool_choice=tool_choice,
                temperature=temperature,
            )
        text = resp.get("reply") or ""
        if text:
            yield ("token", text)
        return text, resp.get("tool_calls")

    return text, tool_calls


def _forced_final_stream(
    llm,
    messages: list[dict],
    openai_tools: list[dict],
    turn: int,
) -> Generator[Tuple[str, Any], None, None]:
    """打转收敛：禁用工具，逼模型用已有信息作答（同样真流式）。

    为什么不直接返 fallback 文案：用户已经等了好几秒，再给一句"抱歉遇到
    复杂度限制"等于白等。此时 messages 里通常已经有足够信息（只是模型在
    反复取同一份），`tool_choice="none"` 一次调用能给出真答案。

    返回 (已下发的文本)；失败返回空串，由调用方退回固定话术。
    """
    stream_events = _stream_events_of(llm)

    if stream_events is None:
        try:
            with llm_gate():
                resp = llm.chat(
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="none",
                    temperature=0.3,   # 已有资料，不需要发散
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"agent_runner 强制收敛调用失败: {type(e).__name__}: {e}")
            return ""
        text = (resp.get("reply") or "").strip()
        if text:
            yield ("token", text)
        return text

    text = ""
    emitted = 0
    try:
        with llm_gate():
            for ev in stream_events(
                messages=messages,
                tools=openai_tools,
                tool_choice="none",
                temperature=0.3,
            ):
                if ev.get("type") != "text":
                    continue  # tool_choice="none" 理论上不会有 tool_calls，来了也忽略
                piece = ev.get("text") or ""
                if not piece:
                    continue
                text += piece
                emitted += 1
                yield ("token", piece)
    except _deadline.DeadlineExceededError:
        raise   # 排队超预算，见 _turn_stream 同名注释
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"agent_runner turn={turn} 收敛流式失败（已发 {emitted} 段）: "
            f"{type(e).__name__}: {str(e)[:120]}",
            extra={"intent": "agent_fc", "turn": turn},
        )
        if emitted:
            return text
        # 一个字都没发出去 → 退非流式再试一次
        try:
            with llm_gate():
                resp = llm.chat(
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="none",
                    temperature=0.3,
                )
        except Exception as e2:  # noqa: BLE001
            logger.warning(f"agent_runner 强制收敛调用失败: {type(e2).__name__}: {e2}")
            return ""
        text = (resp.get("reply") or "").strip()
        if text:
            yield ("token", text)
        return text

    if not text.strip():
        # 流跑完一句没说（同上：声称支持实际没实现）→ 退非流式再试一次，
        # 拿不到才交给调用方走固定话术
        logger.warning(
            f"agent_runner turn={turn} 收敛流空返回，退回非流式重试",
            extra={"intent": "agent_fc", "turn": turn, "stream_fallback": True},
        )
        try:
            with llm_gate():
                resp = llm.chat(
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="none",
                    temperature=0.3,
                )
        except Exception as e2:  # noqa: BLE001
            logger.warning(f"agent_runner 强制收敛调用失败: {type(e2).__name__}: {e2}")
            return ""
        text = (resp.get("reply") or "").strip()
        if text:
            yield ("token", text)
        return text

    return text.strip()


# =============================================================
# 主入口
# =============================================================
def run_stream_agent(
    query: str,
    user_id: Optional[int] = None,
    history: Optional[list[dict]] = None,
    session_id: Optional[str] = None,
) -> Generator[Tuple[str, Any], None, None]:
    """Agent Function Calling 主入口（orchestrator 在 ENABLE_AGENT_FC=True 时调）。

    Args:
        query: 用户问题（必填，非空字符串）
        user_id: 用户 ID（未登录传 None；查订单类工具会被 dispatch 拒绝）
        history: 多轮历史 [{"role", "content"}]（不含 tool_calls）
        session_id: 会话 ID（写操作被状态门禁拦下时建工单按会话去重）

    Yields:
        ("meta", dict) - 元信息：
            - {turn, tool_call: {id, name, arguments}}
            - {turn, tool_result: {id, name, result}}
            - {turn, final: True, tool_used_count}
            - {turn, max_turns_reached: True}
        ("token", str)  - 文本增量（真流式：生成到哪发到哪，不再攒完整段）
        ("done", dict)  - {"answer": str}  最终答案

    Raises:
        ValueError: query 为空
        RuntimeError: ENABLE_AGENT_FC=False（让 orchestrator fallback 到 V1.2）
        Exception: LLM 调用或 dispatch 异常（让 orchestrator fallback）
    """
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    query = query.strip()

    if not settings.ENABLE_AGENT_FC:
        raise RuntimeError(
            "Agent FC 未启用（ENABLE_AGENT_FC=False），"
            "orchestrator 应 fallback 到 V1.2 RAG"
        )

    ctx = ToolContext(user_id=user_id, session_id=session_id)
    llm = get_llm_provider()
    openai_tools = to_openai_tools()
    max_turns = settings.MAX_AGENT_TURNS
    # 工具结果预算：每轮读一次 yaml 没必要（配置不热更新），循环外取一次。
    budget_rules = load_rules()
    guard = LoopGuard(load_loop_rules())   # 每次请求独立记录，不跨会话污染

    system_prompt = _load_system_prompt()
    history_msgs, earlier_summary = _format_history(history)
    if earlier_summary:
        # 要点放在 system 末尾：不进对话流，模型也不会把它当成"用户说过的话"
        system_prompt = f"{system_prompt}\n\n{earlier_summary}"

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(history_msgs)
    messages.append({"role": "user", "content": query})

    for turn in range(1, max_turns + 1):
        logger.info(
            f"agent_runner turn={turn} query={query[:40]!r} user_id={user_id}",
            extra={"intent": "agent_fc", "turn": turn},
        )

        # 总量护栏：单条 slim 只保证"一次调用不超标"，多轮叠加仍会把
        # history 和资料位挤掉。调 LLM 前先把累计长度压回预算内。
        messages, ctx_budget = enforce_total_budget(messages, budget_rules)
        if ctx_budget.get("dropped"):
            yield ("meta", {"turn": turn, "context_budget": ctx_budget})

        # 本轮开始：文本边生成边下发（流式），tool_calls 流末返回。
        # 不再额外发 meta——FC 路径没有 contexts，句级自检本来就没素材可判，
        # 多一条 meta 只会污染消费方的事件契约。
        final_text, tool_calls = yield from _turn_stream(
            llm, messages, openai_tools, "auto", 0.7, turn,
        )

        if tool_calls:
            # LLM 决定调工具：先追加 assistant 消息（含 tool_calls 字段）
            # content 用已发给用户的文本（可能是"我帮您查一下"），空串也行
            messages.append({
                "role": "assistant",
                "content": final_text,
                "tool_calls": tool_calls,
            })

            # 逐个 dispatch 并追加 tool 消息
            all_repeated = True   # 本轮是否全是"已做过的调用"（一个真执行都没有）
            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                tool_name = fn.get("name", "")
                tool_args_raw = fn.get("arguments", "{}") or "{}"
                tool_id = tc.get("id", "")

                yield ("meta", {
                    "turn": turn,
                    "tool_call": {
                        "id": tool_id,
                        "name": tool_name,
                        "arguments": tool_args_raw,
                    },
                })

                # 循环检测：同参重复的只读工具限次、写操作永久拦截。
                # 拦下来时把上次结果连提示一起回灌，模型仍能读到数据。
                decision = guard.check(tool_name, tool_args_raw)
                if not decision.run:
                    yield ("meta", {
                        "turn": turn,
                        "tool_skipped": {
                            "id": tool_id,
                            "name": tool_name,
                            "reason": decision.reason,
                            "times": decision.times,
                        },
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": decision.content,
                    })
                    continue

                all_repeated = False
                result = dispatch(tool_name, tool_args_raw, ctx)
                # meta 用原结果（前端调试要看到工具真返回了什么，_truncate_for_meta
                # 另有 500 字上限）；喂给模型的必须是预算内的版本，否则一次商品
                # 搜索就能吃掉上下文，多轮累积后 history 和资料位全被挤掉。
                slim_result, budget = slim(tool_name, result, budget_rules)
                payload = json.dumps(slim_result, ensure_ascii=False)
                guard.record(tool_name, tool_args_raw, payload)

                yield ("meta", {
                    "turn": turn,
                    "tool_result": {
                        "id": tool_id,
                        "name": tool_name,
                        "result": _truncate_for_meta(result),
                        "budget": budget,
                    },
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": payload,
                })

            # 连续 N 轮全是重复调用 → 判定打转，禁工具逼它收口作答
            guard.end_turn(all_repeated)
            if guard.should_finalize():
                forced = yield from _forced_final_stream(
                    llm, messages, openai_tools, turn,
                )
                if forced:
                    yield ("meta", {
                        "turn": turn,
                        "forced_final": True,
                        "loop_guard": guard.stats(),
                    })
                    yield ("done", {"answer": forced, "forced_final": True})
                    return
                # 收敛失败 → 退出循环，走下方固定话术
                break

            # 进入下一轮 LLM 综合
            continue

        # 无 tool_calls：模型这轮没调工具，答复在上面的流里已经边生成边发给
        # 前端了，这里只补收尾事件（不能再用文字重发一遍，会重复）
        yield ("meta", {
            "turn": turn,
            "final": True,
            "tool_used_count": _count_tool_calls(messages),
        })
        yield ("done", {"answer": final_text})
        return

    # 出口二：达到 MAX_AGENT_TURNS，或打转收敛失败后 break 到这里
    logger.warning(
        f"agent_runner 终止（轮次上限 {max_turns} / 收敛失败）: query={query[:40]!r} "
        f"loop_guard={guard.stats()}",
        extra={"intent": "agent_fc"},
    )
    fallback = "抱歉，处理这个问题时遇到了复杂度限制，请换个更具体的方式描述您的问题。"
    yield ("meta", {"turn": max_turns, "max_turns_reached": True,
                    "loop_guard": guard.stats()})
    # 固定话术不是"生成"出来的，整段一次下发即可——不必再逐字假流
    yield ("token", fallback)
    yield ("done", {"answer": fallback})


__all__ = ["run_stream_agent"]