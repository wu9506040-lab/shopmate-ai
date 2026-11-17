"""
LLMProvider Protocol — 大模型调用抽象

按 CLAUDE.md §9.3.3：业务模块禁止直接调用第三方 LLM SDK。
"""
from typing import Any, Protocol, List, Dict, Generator, Optional, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 能力抽象。

    业务模块通过 `get_llm_provider()` 获取实例，调用 chat / stream_chat。
    当前唯一实现：QwenLLMProvider（基于 DashScope OpenAI 兼容协议）。

    Function Calling 支持（C1 新增）：
        - chat(messages, tools=[...], tool_choice="auto")：同步工具调用
          返回 dict 新增 tool_calls 字段（list[dict]），结构：
              [{"id": "call_xxx", "type": "function",
                "function": {"name": "...", "arguments": "{...json...}"}}]
        - stream_chat_events(...)：流式 FC（文本增量 + 流末 tool_calls 一次集成），
          C2 Agent FC 框架用它做边生成边下发，单轮直接作答不再"伪流式"。
          无此方法的 provider（测试 mock / 老实现）由 agent_runner 自动退回 chat()。
    """

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
    ) -> Dict:
        """同步 chat 调用（支持 Function Calling）。

        Args:
            messages: OpenAI 风格消息列表 [{"role", "content"}, ...]
            model: 模型名（None 用 settings 默认）
            temperature: 0-2
            max_tokens: 输出 token 上限（None = 模型默认）
            tools: OpenAI 风格工具定义列表（None = 不传 tools）。格式:
                [{"type": "function", "function": {"name", "description", "parameters"}}]
            tool_choice: "auto" / "none" / {"type": "function", "function": {"name": "..."}}
                None = 不传（由模型默认行为决定）

        Returns:
            {
                "reply": str,
                "model": str,
                "usage": {"prompt_tokens", "completion_tokens", "total_tokens"},
                "tool_calls": Optional[List[Dict]]  # 模型决定调用工具时非空
            }

        Raises:
            BadRequestError / AuthenticationError / PermissionDeniedError: 业务错（不重试）
            CircuitOpenError: 断路器开路（上游应降级到兜底）
            Exception: 重试耗尽后透传最后一次异常
        """
        ...

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
        on_usage: Optional[Any] = None,
    ) -> Generator[str, None, None]:
        """流式 chat 调用。

        仅对 client.create() 阶段做 retry + 断路器。流式中途断连仅 log，不重试。

        Note:
            tools/tool_choice 参数保留接口一致性，但**当前 QwenLLMProvider 不真正透传
            给 DashScope 流式端点**。Agent FC 场景请用 stream_chat_events()。

        Yields:
            文本片段（可能为空字符串，已过滤）
        """
        ...

    # 可选能力（刻意不写进 Protocol 本体）：
    #   stream_chat_events(messages, *, tools, tool_choice, ...)
    #       -> Generator[{"type": "text"|"tool_calls", ...}]
    # 流式 FC 的出口。**不是所有 provider 都必须有**——agent_runner 用
    # getattr 探测，没有就退回非流式 chat()。写进 Protocol 会让
    # runtime_checkable 把所有老实现（含测试 fake）判成不合规，那就等于
    # 逼着每个 provider 都实现一套 chunk 状态机。