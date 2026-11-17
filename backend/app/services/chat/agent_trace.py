"""Agent 决策追踪（V20 亮点 3）：每轮对话一条结构化轨迹。

为什么需要：
- 此前 Agent 的路由 / 重试 / 降级信息散落在日志与 metrics 里，无法按会话回放，
  也算不出"自动处理率 / 转人工率"这类业务价值指标（亮点 4 的数据源）。
- 设计取舍：不做 OpenTelemetry（重、引依赖），一期用 JSONL 落盘——
  一轮一行 JSON，grep/jq 即可分析；后续量大再换 MySQL/ClickHouse，字段结构不变。

可靠性约定（与 checkpointer 同一哲学）：
- trace 是观测项不是业务项——**flush 里的任何异常都吞掉打 warning**，
  绝不影响主应答链路。
- 同步写（asyncio.to_thread 由调用方决定），文件 append 模式，多进程安全靠
  单行 O_APPEND 原子性（< PIPE_BUF 一行不会被截断）。

一条 trace 的字段（一问一答一轮）：
  ts, session_id, user_id, channel, query_len,
  query, answer, answer_len,                    # 问答正文（脱敏+截断，可开关）
  intent, intent_method, intent_confidence,     # 意图路由
  retrieval_hits, retrieval_n,                  # 检索层快照（top5 来源+分数+版本）
  citations,                                    # 答案引用来源
  usage,                                        # LLM token 消耗（流式路径暂缺）
  cache_hit, degraded, outcome,                 # 路径与结果
  latency_ms, error_type

为什么存 query/answer 正文：
  只有 query_len 时，badcase 只能靠"重新发一次请求"复现，而 LLM 输出有方差，
  复现到的往往不是当时那句答案——P01 尺码依据引用错行列就是复现不出来的。
  落正文后，评分口径的变化、期望值是否有误，都能直接对当时原文判定。

badcase 分层定位约定（七维评测）：
  retrieval_hits 缺期望文档 → 检索层(D1)；hits 有但答案错 → 生成层(D2/D7)；
  intent 错 → 路由层(D3)；outcome=error/degraded → 稳定性(D4)；
  usage/latency 异常 → 成本(D5)；越权类 → 安全(D6，专项 E2E 断言)。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()

# 正文落盘前的脱敏：手机号、订单号不落明文（trace 会被拷去排查/发人看）
_RE_PHONE = re.compile(r"\b1[3-9]\d{9}\b")
_RE_ORDER = re.compile(r"\bORD[0-9A-Z]{8,}\b")


def _redact(text: str) -> str:
    """遮蔽手机号与订单号，保留可定位信息。"""
    if not text:
        return ""
    text = _RE_PHONE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], text)
    text = _RE_ORDER.sub(lambda m: m.group(0)[:7] + "***", text)
    return text


class AgentTrace:
    """单轮对话的轨迹收集器：chat 主流程各节点 mark，结束统一 flush。"""

    __slots__ = (
        "_data", "_start", "_done",
    )

    def __init__(self, session_id: str, user_id: Any, channel: Optional[str], query: str):
        from app.core.config import settings

        self._start = time.perf_counter()
        self._done = False
        self._data: dict[str, Any] = {
            "ts": int(time.time() * 1000),
            "session_id": session_id,
            "user_id": user_id,
            "channel": channel or "web",
            "query_len": len(query or ""),
            "trace_enabled": bool(getattr(settings, "AGENT_TRACE_ENABLED", True)),
        }
        # 正文是否落盘：默认开（badcase 回放必需），生产可关掉只留长度
        if getattr(settings, "AGENT_TRACE_STORE_TEXT", True) and query:
            limit = int(getattr(settings, "AGENT_TRACE_TEXT_MAX", 600))
            self._data["query"] = _redact(str(query))[:limit]

    # ---------- 各阶段 mark（全部 best-effort，不抛异常） ----------

    def mark(self, key: str, value: Any) -> None:
        """记录一个字段（intent / degraded / cache_hit ...）。"""
        try:
            self._data[key] = value
        except Exception:  # pragma: no cover —— __slots__/序列化异常兜底
            pass

    def mark_intent(self, snap: Optional[dict]) -> None:
        """记录意图路由快照（chat.py 消费的 _intent 内部事件）。"""
        if not snap:
            return
        try:
            self._data["intent"] = snap.get("intent") or snap.get("primary")
            self._data["intent_method"] = snap.get("method")
            self._data["intent_confidence"] = snap.get("confidence")
            if snap.get("upgraded"):
                self._data["intent_upgraded"] = True
        except Exception:  # noqa: BLE001
            pass

    def mark_retrieval(self, contexts: list, scores: list) -> None:
        """记录检索层快照（检索层定位依据）。

        contexts 是 meta 事件里的来源列表（title/source/revision 等），
        scores 是对应相关性分数。只留 top5，防 trace 行膨胀：
        badcase 定位只需要「期望文档进没进头部、排名多少、分数多少」。
        """
        try:
            hits = []
            for i, c in enumerate(list(contexts or [])[:5]):
                item = {
                    "rank": i + 1,
                    "src": (c.get("source") or c.get("doc_key") or "")[:80],
                    "score": None,
                }
                if isinstance(scores, list) and i < len(scores):
                    try:
                        item["score"] = round(float(scores[i]), 4)
                    except (TypeError, ValueError):
                        pass
                rev = c.get("revision") or c.get("price_version")
                if rev:
                    item["rev"] = str(rev)[:16]
                hits.append(item)
            if hits:
                self._data["retrieval_hits"] = hits
                self._data["retrieval_n"] = len(contexts or [])
        except Exception:  # noqa: BLE001 —— 观测层绝不影响应答
            pass

    def mark_usage(self, usage: Optional[dict]) -> None:
        """记录 LLM token 消耗（成本效率数据源）。

        流式路径从 SSE 流末帧解析（请求侧开了 stream_options）；
        兼容端点不支持该参数时本字段不存在——评测器按口径降级并标注。
        """
        if not usage:
            return
        try:
            # 兼容两种口径：qwen 流末帧解析后给的是短名 prompt/completion/total，
            # 其他 provider 直接透传 OpenAI 原始的 prompt_tokens/completion_tokens/total_tokens
            self._data["usage"] = {
                "prompt": usage.get("prompt_tokens", usage.get("prompt")),
                "completion": usage.get("completion_tokens", usage.get("completion")),
                "total": usage.get("total_tokens", usage.get("total")),
            }
        except Exception:  # noqa: BLE001
            pass

    def mark_citations(self, contexts: list) -> None:
        """记录答案引用的来源列表（D2 证据引用：引用与答案一致性）。"""
        try:
            cites = [
                (c.get("source") or c.get("doc_key") or "")[:80]
                for c in (contexts or [])[:5]
            ]
            if cites:
                self._data["citations"] = cites
        except Exception:  # noqa: BLE001
            pass

    def mark_answer(self, answer: str) -> None:
        """记录本轮实际下发的答案正文（生成层定位依据）。

        落的是「实际 yield 给前端的文本」而非模型原始 token——自检拦掉的句子、
        段间空行都在里面，和用户看到的完全一致，才能拿去判定答案完整性。
        answer_len 无论开关都记，成本分析要用。
        """
        try:
            text = str(answer or "")
            self._data["answer_len"] = len(text)
            from app.core.config import settings
            if getattr(settings, "AGENT_TRACE_STORE_TEXT", True) and text:
                limit = int(getattr(settings, "AGENT_TRACE_TEXT_MAX", 600))
                self._data["answer"] = _redact(text)[:limit]
        except Exception:  # noqa: BLE001 —— 观测层绝不影响应答
            pass

    # ---------- flush：唯一落盘点 ----------

    def flush(self, outcome: str, error_type: Optional[str] = None) -> None:
        """把轨迹写入 JSONL。任何异常吞掉——观测层绝不影响应答。"""
        if self._done:
            return
        self._done = True
        try:
            from app.core.config import settings

            if not getattr(settings, "AGENT_TRACE_ENABLED", True):
                return

            self._data["outcome"] = outcome
            if error_type:
                self._data["error_type"] = error_type
            self._data["latency_ms"] = round((time.perf_counter() - self._start) * 1000, 1)

            trace_dir = Path(getattr(settings, "AGENT_TRACE_DIR", "data/agent_traces"))
            trace_dir.mkdir(parents=True, exist_ok=True)
            path = trace_dir / f"trace-{time.strftime('%Y%m%d')}.jsonl"

            line = json.dumps(self._data, ensure_ascii=False, default=str)
            with _write_lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 —— 观测层异常只打日志
            logger.warning("[agent_trace] flush 失败（忽略）: %s", e)
