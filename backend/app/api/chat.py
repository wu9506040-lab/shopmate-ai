"""
Chat HTTP 接口层（多源融合 + 多轮对话 + MySQL write-through §11 + 流式输出 §14）

按 §6 规则：
- api/ 只负责路由 + 参数解析 + 调 services
- 不写业务逻辑（M4 起编排全在 services/synthesizer.py）

§10 起：可选 user 上下文
§11 起：write-through Redis + MySQL，audit 上报
§14 起：SSE 流式输出（POST /chat 升级为 text/event-stream）
起：Synthesizer.run_stream 替代 V1.2 统一 RAG，按意图分派到不同 service/tool
起：SSE heartbeat + 客户端断开检测（健壮性加固）

实现：
    POST /chat → load_history_with_fallback → Synthesizer.run_stream(query, user_id, history)
              → 边收 token 边 yield SSE → 收 done 后 write-through（Redis + MySQL + audit）
              → 全程 heartbeat 保活（30s 间隔）+ 断开检测
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Generator, Optional, Tuple, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user_optional
from app.core.config import settings
from app.core.context import set_session_id, set_user_id  # M8
from app.core.embedding import begin_embed_memo  # (2.2) 请求级 embedding 复用
from app.models.user import User
from app.schemas.chat import ChatRequest, ResumeRequest
from app.services.audit_service import try_log_action
from app.services.behavior_monitor import behavior_monitor  # P2
from app.services.chat.orchestrator import Synthesizer # ：从 services/synthesizer 切到 services/chat/orchestrator
from app.services.chat.prompt_assembler import _build_meta_contexts # ：原 services/synthesizer
from app.services.escalation_service import (  # 转人工兜底
    EscalationReason,
    detect_handoff_keyword,
    detect_p0_escalate,
    get_escalation_service,
    get_p0_category_info,
)
from app.services.metrics import metrics  # M8
from app.services.policy_service import PolicyService
# policy_query 路径 order_no 归属校验
# 与 P2-4 (order_context_resolver.py) 同源设计：OrderTool.get_order_by_no
from app.tools.order_tool import OrderTool
# SSE Resume：stream checkpoint 写入/读取/清理
from app.services.redis_store import (
    STREAM_MAX_RESUME_TIMES,
    del_stream_checkpoint,
    get_stream_checkpoint,
    increment_resume_count,
    set_stream_checkpoint,
)
from app.services.session_service import (
    ANONYMOUS_USER_ID,
    append_exchange,
    generate_session_id,
    load_history_with_fallback,
    persist_to_mysql,
)
# 越权防护（IDOR）：续聊前校验 session 归属
from app.services.mysql_store import get_conversation_owner
# 流量分类：测试会话在写入期打标，统计侧不再各写一份前缀正则
from app.services.session_kind import ensure_session_kind
# P2 长程记忆：每轮 done 后异步更新 user_profiles（best-effort）
from app.services import badcase_service  # guard 误杀留证入池
from app.services import profile_service
# 记忆契约：失败回答不当事实 / 剥引用角标 / 多轮不查缓存
from app.services.chat.memory_contract import can_use_cache, prepare_history_safe
# 会话上下文写侧：每轮记录 last_intent / current_order_no / resolved_orders
from app.services.context.turn_recorder import record_turn_context
# L3 跨会话长期记忆：只从 biz 会话学，且跨会话确认两次才生效
from app.services.l3_capture import capture_turn
from app.services.guard import guard as input_guard
from app.services.intent_service import IntentService
from app.services.response_cache import get_cached_answer, put_cached_answer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

# heartbeat 间隔（秒）—— 30s 是 nginx 默认 proxy_read_timeout（60s）的一半
SSE_HEARTBEAT_INTERVAL = 30.0


def _client_ip(request: Request) -> Optional[str]:
    """取客户端 IP（优先 X-Forwarded-For，再 client.host）"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _user_agent(request: Request) -> Optional[str]:
    """取 UA，截断 500 字符"""
    ua = request.headers.get("user-agent", "")
    return ua[:500] if ua else None


def _sse_format(data: dict, seq: Optional[int] = None) -> str:
    """格式化为 SSE data 行（每条 event 以 \\n\\n 结束）

    SSE Resume：传 seq 时加 `id: {seq}\\n` 行（SSE 标准 Last-Event-ID 协议）。
    seq 为 None 时保持原格式（向后兼容 guard/cache 等不需要 resume 的路径）。
    """
    if seq is not None:
        return f"id: {seq}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk_text(text: str, size: int = 10) -> list[str]:
    """按字符切片，模拟 LLM token 流（前端打字机效果）
    中文按字符切（不拆字节），英文按 size 字符切
    """
    if not text:
        return []
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + size])
        i += size
    return chunks


# =============================================================
# POST /chat - SSE 流式版本（§14 + M7 heartbeat）
# =============================================================
@router.post(
    "/chat",
    summary="RAG 多轮问答（SSE 流式）",
    description=(
        "基于知识库的检索增强问答，支持多轮会话。"
        "返回 text/event-stream，事件类型：meta / token / heartbeat / done / error / closed。"
        "需要 httpOnly Cookie 鉴权（自动通过浏览器携带）。"
    ),
    response_class=StreamingResponse,
)
async def chat(
    request: Request,
    payload: ChatRequest,
    user: Optional[User] = Depends(get_current_user_optional),
):
    """
    RAG Chat 端点（流式版本）

    SSE 协议（自定义 JSON 事件）：
        data: {"type":"meta","contexts":[...],"scores":[...]}\\n\\n
        data: {"type":"token","text":"你"}\\n\\n
        ...
        data: {"type":"heartbeat","ts":1234567890}\\n\\n   ← M7：30s 间隔
        data: {"type":"done","session_id":"..."}\\n\\n
        data: {"type":"closed"}\n\n                            ← M7：服务端优雅关闭

    健壮性（M7）：
        - heartbeat 事件：每 30s 发送，告知客户端"连接还活着"
        - 客户端断开检测：asyncio.CancelledError → 写审计 + 跳出循环
        - graceful close：正常结束时发 closed 事件（前端可识别）
    """
    # 1. 决定 session_id（缺失则新建）
    session_id = payload.session_id or generate_session_id()

    # 1.2 流量分类打标（写入期事实，替代统计侧的运行期正则猜测）。
    # 评测/smoke/排障脚本带 `X-Traffic-Kind: eval|smoke|debug` 显式声明；
    # 没带的用唯一一份前缀正则兜底。落库失败不影响对话本身。
    traffic_kind = (request.headers.get("x-traffic-kind") or "").strip()
    await asyncio.to_thread(ensure_session_kind, session_id, traffic_kind)

    # 1.5 会话归属校验（IDOR 越权防护）。
    # 之前 /chat 从不校验 payload.session_id 归属：登录用户传入他人 session_id
    # 就能读到对方历史（注入本轮 prompt 上下文，可诱导复述）并向其会话写消息。
    # 规则：
    #   - 匿名（visitor）不做校验 —— 匿名会话本就共享 ANONYMOUS_USER_ID（已知
    #     演示留白，历史行为不变）；xianyu 渠道也是匿名身份，同样放行
    #   - 会话不存在（新建）或归属匿名 → 放行（登录用户接管自己的新会话）
    #   - 会话属于其他登录用户 → 404（不暴露存在性，文案与 ConversationService 一致）
    if (
        payload.session_id
        and user is not None
        and user.id != ANONYMOUS_USER_ID
    ):
        owner_id = await asyncio.to_thread(get_conversation_owner, payload.session_id)
        if owner_id is not None and owner_id != ANONYMOUS_USER_ID and owner_id != user.id:
            logger.warning(
                f"/chat 越权会话访问被拒: user={user.id} owner={owner_id} "
                f"session={payload.session_id[:12]}..."
            )
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")

    # P3（Java+Python 三部分架构）：落库权归属判定。
    # Java 网关转发时带 X-Platform-Persist: java → 平台层在 done 帧落库并回填
    # assistant_message_id，本服务跳过 MySQL 写穿（单一写入者，避免双写）。
    # 直连 Python（无网关/老链路）仍保留原有写穿行为，向后兼容。
    platform_owns_persist = (
        (request.headers.get("x-platform-persist") or "").strip().lower() == "java"
    )

    user_id = user.id if user else ANONYMOUS_USER_ID
    user_ctx = (
        f"user={user.username}(id={user.id})" if user else "user=anonymous"
    )
    ip = _client_ip(request)
    ua = _user_agent(request)

    # 闲鱼渠道——仍走原 RAG 路径，但**前置注入** BuyerProfile + ItemPrompt 到 system_prompt
    if getattr(payload, "channel", None) == "xianyu" and payload.buyer_unb:
        _xianyu_inject_context(payload)  # 把 BuyerProfile + ItemPrompt 注入 payload.context_block
    elif getattr(payload, "channel", None) == "xianyu" and not payload.buyer_unb:
        # 兜底：channel=xianyu 但缺 buyer_unb（daemon 没填）→ 走通用 RAG
        pass

    # 把 session_id / user_id 写入 ContextVar（日志自动带）
    set_session_id(session_id)
    set_user_id(user_id if user else None)

    # 商品上下文绑定：从商品页进入会话时，整轮检索只认该款切片，
    # 避免「问 A 款命中 B 款参数/尺码」。款号优先取显式 model，
    # 缺省时从 SKU「款号-尺码」格式推导；都不满足则不隔离（保持通用检索）。
    from app.services.policy_service import set_kb_model
    import re as _re

    _model = getattr(payload, "model", None)
    if not _model and payload.sku and "-" in payload.sku:
        _candidate = payload.sku.rsplit("-", 1)[0]
        if _re.fullmatch(r"FS\d{6}", _candidate):
            _model = _candidate

    # 多轮指代：用户说「它 / 这个 / 那款」时，把会话里最近咨询的商品补回来。
    # 补的是**上下文绑定**，payload.query 一个字都不改——改写用户问题会改变
    # 意图，检索再准也是答另一件事。
    from app.services import reference_resolver as _ref
    if _model:
        # 用户这轮明确指了商品，记下来供后续指代使用
        await asyncio.to_thread(_ref.remember, session_id, _model, payload.sku)
    else:
        _ref_hit = await asyncio.to_thread(
            _ref.resolve, payload.query, session_id
        )
        if _ref_hit:
            _model = _ref_hit["model"]
            if not payload.sku and _ref_hit.get("sku"):
                payload.sku = _ref_hit["sku"]
            logger.info(
                f"/chat 指代消解: query={payload.query[:30]!r} "
                f"-> model={_model} sku={payload.sku} "
                f"({_ref_hit.get('reason')})"
            )

    set_kb_model(_model)
    if _model:
        logger.info(f"/chat 商品切片绑定: model={_model} sku={payload.sku}")

    # 2. 预加载历史（同步 IO，asyncio.to_thread 异步化）
    try:
        history = await asyncio.to_thread(
            load_history_with_fallback, session_id
        )
    except Exception as e:
        logger.exception(f"/chat history 加载失败: session={session_id[:12]}... {user_ctx}")
        raise HTTPException(status_code=500, detail=f"历史加载失败: {type(e).__name__}")

    # 2.1 记忆契约：失败回答不当事实 + 剥引用角标 + 按轮/字符预算裁剪
    # 线上体检：83% 的答案带 [n]、18% 是兜底话术，无差别进 history 会自我污染；
    # 且原实现按 20 条全量拼接，一条长答案就能把资料位挤掉。
    # 放在这里而不是 orchestrator：改写/意图/装配都从同一个 history 读，处理一次就够。
    # known_facts 用来压掉摘要里的旧值：买家改口后只有 L2 是准的，
    # 摘要（从被裁旧轮次抽的）若还写着旧尺寸，两处同时进 prompt，
    # 模型答哪个全看运气。一次 Redis GET，失败就退回"不过滤"。
    known_facts: dict = {}
    try:
        if session_id and user_id:
            from app.services.context import get_context_service

            known_facts = get_context_service().load(session_id, user_id).facts or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"/chat 读取 L2 事实失败（摘要不做冲突过滤）: {e}")
    history = prepare_history_safe(history, known_facts=known_facts)

    logger.info(
        f"/chat start: session={session_id[:12]}..., "
        f"history_len={len(history)} {user_ctx}"
    )

    # 2.5 V15：会话入口用户上下文预加载（best-effort，不阻塞主流程）
    #   - 加载最近 5 单（含产品）+ 画像 → 注入 LLM prompt
    #   - 灰度开关 ENABLE_USER_CONTEXT_PRELOAD 默认关，验证后开
    user_context_block = ""
    if settings.ENABLE_USER_CONTEXT_PRELOAD and user_id and user_id != ANONYMOUS_USER_ID:
        try:
            from app.services import user_context_preloader as _ucp
            preload_data = await asyncio.to_thread(_ucp.preload, user_id)
            user_context_block = _ucp.to_prompt_block(preload_data)
            logger.info(
                f"/chat 上下文预加载: user_id={user_id}, "
                f"orders={len(preload_data.get('orders', []))}, "
                f"profile_len={len(preload_data.get('profile_text', ''))}"
            )
        except Exception as e:
            logger.warning(f"/chat 上下文预加载 失败（放行）: user_id={user_id}, {e}")
            user_context_block = ""

    # 3. 异步 SSE 生成器（async generator + heartbeat + 断开检测）
    async def event_generator():
        # （2.2）：请求级 embedding 复用起点——guard L2 / 语义缓存读写 /
        # 检索共享同一 query 向量（此前同请求同文本最多真实调 4 次 API）。
        # contextvar 随本 Task 结束自然消亡，无需显式 reset。
        begin_embed_memo()
        # 请求级超时预算入口——整 90s 墙钟，含 rewrite/intent/
        # 检索/rerank/decide/synthesize 所有阶段；client 层统一 clamp_timeout
        # + check()。budget 不承诺秒杀 in-flight 调用（那是 cancel 级成本
        # 不配），但**任何新阶段的入口都会被关卡拦下**。
        from app.core import deadline as _deadline
        from app.core.config import settings as _s
        _deadline.start_budget(_s.CHAT_DEADLINE_SECONDS)

        # 异常行为监控（在最早期记一次，含被 guard 拦的）
        # — 不阻塞业务（Redis 异常放行），只告警
        behavior_monitor.record_request(
            ip=ip, user_id=user_id,
            sku=payload.sku, order_no=payload.order_no,
        )

        # Agent 决策追踪——一轮一条 JSONL（业务指标的来源）
        from app.services.chat.agent_trace import AgentTrace

        agent_trace = AgentTrace(session_id, user_id, getattr(payload, "channel", None), payload.query)

        # InputGuard 3 层防御（在最早期拦住垃圾/闲聊/重复，省 LLM token）
        # 修正：channel=xianyu 时 bypass guard（已注入 BuyerProfile + ItemPrompt，guard 误判率高）
        is_xianyu = getattr(payload, "channel", None) == "xianyu"
        guard_result = (
            input_guard.check(payload.query, user_id, has_history=bool(history))
            if not is_xianyu else None
        )
        # L3 软限：买家 1min 内重复同一句 >3 次 → 不拦，但本轮不查缓存。
        # 为什么：重复说明上一轮没解决，缓存只会把同一个答案再喂一遍；
        # 而拦掉等于把最需要帮助的人挡在门外。
        guard_degraded = bool(getattr(guard_result, "degraded", False))
        if guard_degraded:
            logger.info(f"/chat guard 软限放行（本轮跳缓存）: {user_ctx}")
            # 留证：这类问题比 guard 误杀更能说明答案质量
            badcase_service.enqueue_repeat_soft_safe(
                query=payload.query, session_id=session_id, user_id=user_id,
            )
        if guard_result is not None and not guard_result.allowed:
            # 黑名单静默不响应；其他走固定话术
            yield _sse_format({
                "type": "meta",
                "intent": "blocked",
                "entities": {"order_no": None, "sku": None, "keywords": []},
                "contexts": [],
                "scores": [],
                "guard_layer": guard_result.layer,
                "guard_reason": guard_result.reason,
                # 违禁品类目（涉毒/枪支弹药/…），前端不展示，工单与审计按它筛
                "guard_category": guard_result.category,
            })
            if guard_result.response:
                # 分段 yield，模拟正常 token 流（前端能正常渲染）
                for chunk in _chunk_text(guard_result.response, size=10):
                    yield _sse_format({"type": "token", "text": chunk})
            yield _sse_format({"type": "done", "session_id": session_id})
            yield _sse_format({"type": "closed"})
            try_log_action(
                user=user, action="chat_guard_blocked", target_type="session",
                target_id=session_id, ip=ip, user_agent=ua,
                detail={
                    "guard_layer": guard_result.layer,
                    "guard_reason": guard_result.reason,
                    "query_len": len(payload.query),
                },
            )
            # operation_log 不记正文——guard 误杀的证据只能落 badcase 池。
            # 自开 session、失败显形（metrics+日志），绝不阻断拦截应答。
            badcase_service.enqueue_guard_blocked_safe(
                query=payload.query, session_id=session_id, user_id=user_id,
                layer=guard_result.layer or "", reason=guard_result.reason or "",
            )
            logger.info(
                f"/chat guard blocked: layer={guard_result.layer} "
                f"reason={guard_result.reason} {user_ctx}"
            )
            agent_trace.flush("blocked")
            return

        # 换款候选判断已后移：必须排在 P0 风险闸与「主动转人工」之后。
        # 理由：「我要换款，不然我投诉」这类混合句若先弹商品卡片，会直接压掉
        # 转人工通道——风险处置的优先级必须高于换款引导。

        # P0 高风险关键词检测（投诉/赔付/质量/主动要人工）
        # 放在 detect_handoff_keyword 之前：4 类优先级更精准，命中即带 priority/category 元数据。
        # 未命中再走下面的 detect_handoff_keyword（原 9 词兜底）。
        p0_hit = detect_p0_escalate(payload.query)
        if p0_hit:
            p0_category, p0_keyword = p0_hit
            p0_priority, p0_label = get_p0_category_info(p0_category)
            escalation = get_escalation_service()
            handoff_payload = escalation.handoff(
                reason=EscalationReason.USER_REQUESTED,
                user_id=user_id,
                session_id=session_id,
                history=history,
                intent_result=None,
                failure_context=None,
                priority=p0_priority,
                category=p0_label,
                matched_keyword=p0_keyword,
                detected_category=p0_category,
            )
            yield _sse_format({
                "type": "meta",
                "intent": "handoff",
                "entities": {"order_no": None, "sku": None, "keywords": []},
                "contexts": [],
                "scores": [],
                "handoff": handoff_payload.to_dict(),
            })
            for chunk in _chunk_text(
                f"{handoff_payload.reason_label}（工单号 {handoff_payload.handoff_id}），人工客服会尽快联系您～",
                size=10,
            ):
                yield _sse_format({"type": "token", "text": chunk})
            yield _sse_format({"type": "done", "session_id": session_id})
            yield _sse_format({"type": "closed"})
            try_log_action(
                user=user, action="chat_handoff_p0", target_type="session",
                target_id=session_id, ip=ip, user_agent=ua,
                detail={
                    "handoff_id": handoff_payload.handoff_id,
                    "priority": p0_priority,
                    "category": p0_category,
                    "matched_keyword": p0_keyword,
                },
            )
            logger.info(
                f"/chat handoff (P0 {p0_category}): handoff_id={handoff_payload.handoff_id} "
                f"kw={p0_keyword} session={session_id[:12]}... {user_ctx}"
            )
            agent_trace.mark("degraded", True)
            agent_trace.flush("handoff_p0")
            return

        # 转人工关键词检测（在 IntentService.classify 之前，跳过 LLM 调用）
        if detect_handoff_keyword(payload.query):
            escalation = get_escalation_service()
            handoff_payload = escalation.handoff(
                reason=EscalationReason.USER_REQUESTED,
                user_id=user_id,
                history=history,
                intent_result=None,
                failure_context=None,
            )
            yield _sse_format({
                "type": "meta",
                "intent": "handoff",
                "entities": {"order_no": None, "sku": None, "keywords": []},
                "contexts": [],
                "scores": [],
                "handoff": handoff_payload.to_dict(),
            })
            for chunk in _chunk_text(
                f"{handoff_payload.reason_label}（工单号 {handoff_payload.handoff_id}），人工客服会尽快联系您～",
                size=10,
            ):
                yield _sse_format({"type": "token", "text": chunk})
            yield _sse_format({"type": "done", "session_id": session_id})
            yield _sse_format({"type": "closed"})
            try_log_action(
                user=user, action="chat_handoff_user_requested", target_type="session",
                target_id=session_id, ip=ip, user_agent=ua,
                detail={"handoff_id": handoff_payload.handoff_id},
            )
            logger.info(
                f"/chat handoff (user_requested): handoff_id={handoff_payload.handoff_id} "
                f"session={session_id[:12]}... {user_ctx}"
            )
            agent_trace.mark("degraded", True)
            agent_trace.flush("handoff_user")
            return

        # 换款意图：从真实在售款给候选卡片，不反问「您要换哪一个」
        # 位置：P0 风险闸 + 主动转人工之后（风险优先于换款引导）
        from app.services.product_switch import match_product_choices
        try:
            choices = match_product_choices(payload.query, _model)
        except Exception as e:
            logger.warning(f"换款候选匹配失败（放行）: {e}")
            choices = None
        if choices:
            yield _sse_format({
                "type": "product_choices",
                "choices": choices,
                "current_model": _model,
            })
            hint = "为你找到这几款，点一张卡片就能切换咨询的商品～"
            for chunk in _chunk_text(hint, size=10):
                yield _sse_format({"type": "token", "text": chunk})
            yield _sse_format({"type": "done", "session_id": session_id})
            yield _sse_format({"type": "closed"})
            agent_trace.mark("product_switch_offered", len(choices))
            agent_trace.flush("product_choices")
            logger.info(
                f"/chat 换款候选: {_model} -> {[c['model'] for c in choices]}"
            )
            return

        # refund_query 不进缓存（meta refundable/reason 是 order 相关的，不能复用）
        try:
            pre_intent = await asyncio.to_thread(IntentService.classify, payload.query)
            # classify() 新结构有 primary；兼容旧 intent 别名（=primary）
            intent_for_cache = pre_intent.get("primary", pre_intent.get("intent", "policy_query"))
        except Exception:
            pre_intent = None
            intent_for_cache = "policy_query"

        # policy_query + order_no entity → 校验归属
        # 防止 policy_query 路径绕过 OrderContextResolver 的归属校验逻辑
        # 与 P2-4 (order_context_resolver.py) 同源：OrderTool.get_order_by_no(user_id, order_no)
        # 注意：
        #   - 匿名用户跳过（与 Resolver 一致，避免账号枚举）
        #   - 仅 policy_query 触发；refund_query/order_query 已走 Resolver 内部校验
        #   - 校验失败 → SSE not_found 流（meta.intent="not_found" + 中文提示）
        #   - 异常 → logger.warning 放行（best-effort，不阻塞正常 policy_query）
        if intent_for_cache == "policy_query" and user_id and user_id != ANONYMOUS_USER_ID and pre_intent:
            pre_entities_check = (pre_intent.get("entities") or {})
            order_no_check = pre_entities_check.get("order_no")
            if order_no_check:
                try:
                    owned_order = await asyncio.to_thread(
                        OrderTool.get_order_by_no, user_id, order_no_check
                    )
                except Exception as e:
                    logger.warning(
                        f"/chat policy_query 归属校验异常（放行）: "
                        f"order_no={order_no_check}, user_id={user_id}, {e}"
                    )
                    owned_order = "ERROR"  # 哨兵值：异常时跳过 NOT_FOUND 判定，继续正常 policy_query
                if owned_order is None:
                    # NOT_FOUND：订单不属于当前 user 或不存在
                    yield _sse_format({
                        "type": "meta",
                        "intent": "not_found",
                        "entities": pre_entities_check,
                        "contexts": [],
                        "scores": [],
                        "resolver_action": "not_found",
                        "resolver_reason": "policy_query_order_not_owned_v10_a",
                    })
                    not_found_msg = (
                        f"抱歉，当前账号下未查到订单 {order_no_check}。"
                        "我已按账号范围核对，可直接前往订单页查看，或由我转人工协助。"
                    )
                    for chunk in _chunk_text(not_found_msg, size=10):
                        yield _sse_format({"type": "token", "text": chunk})
                    yield _sse_format({"type": "done", "session_id": session_id})
                    yield _sse_format({"type": "closed"})
                    try_log_action(
                        user=user, action="chat_policy_not_found_v10a", target_type="session",
                        target_id=session_id, ip=ip, user_agent=ua,
                        detail={
                            "order_no": order_no_check,
                            "query_len": len(payload.query),
                        },
                    )
                    logger.info(
                        f"/chat policy_query not_found: order_no={order_no_check} "
                        f"session={session_id[:12]}... {user_ctx}"
                    )
                    return

        # 响应缓存（exact + semantic），10min 内同 query 不再调 LLM
        # 仅对 policy_query 启用，refund_query 必须走 LangGraph 保证 meta 正确
        #
        # scope（2026-11）：缓存必须按商品隔离。policy_query 的 prompt 里带
        # 【当前商品】参数和尺码表，同一用户切商品后问同一句，原来会命中
        # 上一款的答案（在毛衣页问洗护，答的却是夹克的聚酯纤维）。
        # 粒度用 SKU（同款不同码问「胸围多少」不能串），无 SKU 时退化到
        # 款号，两者都没有（店铺入口纯政策问）才落 global。
        _cache_scope = payload.sku or (
            f"model-{_model}" if _model else None
        )
        cached_hit = None
        if (
            intent_for_cache == "policy_query"
            and can_use_cache(history)
            and not guard_degraded
        ):
            cached_hit = await asyncio.to_thread(
                get_cached_answer, payload.query, user_id, _cache_scope
            )
        if cached_hit:
            # policy_query 才进缓存（refund_query 已在前置分类跳过）
            cached_answer = cached_hit["answer"]
            cached_entities = pre_intent.get("entities", {"order_no": None, "sku": None, "keywords": []})
            # 引用与答案同源：写入时把 contexts/scores 和答案一起存了，
            # 命中直接用快照，不再重新检索。否则答案讲 A 款、引用列 B 款，
            # 前端展示的来源和正文会互相对不上。
            cache_contexts = cached_hit.get("contexts") or []
            cache_scores = cached_hit.get("scores") or []
            if not cache_contexts:
                # 改版前写入的旧条目没有引用快照，退化成重新检索一次凑来源。
                # TTL 只有 10 分钟，这段兼容代码会自动失去意义。
                try:
                    cache_policy_docs = await asyncio.to_thread(
                        PolicyService.search_policy, payload.query, 5, skip_rerank=True
                    )
                    cache_contexts, cache_scores = _build_meta_contexts(policy_docs=cache_policy_docs)
                except Exception as e:
                    logger.warning(f"cache_hit 拿 contexts 失败（放行）: {e}")
            yield _sse_format({
                "type": "meta",
                "intent": intent_for_cache,
                "intent_method": "cache_hit",
                "entities": cached_entities,
                "policy_hits": len(cache_contexts),  # 实际 RAG 命中数，不是缓存标记
                "contexts": cache_contexts,
                "scores": cache_scores,
            })
            for chunk in _chunk_text(cached_answer, size=10):
                yield _sse_format({"type": "token", "text": chunk})
            yield _sse_format({"type": "done", "session_id": session_id})
            yield _sse_format({"type": "closed"})
            # 写穿透（best-effort，让历史能复用）
            try:
                await asyncio.to_thread(
                    append_exchange, session_id, payload.query, cached_answer
                )
            except Exception as e:
                logger.warning(f"cache 命中后写历史失败: {e}")
            # 缓存命中也得落 MySQL：此前这个分支只写 Redis 热历史，
            # 库里这一轮问答是缺的（客服工作台看历史会断轮），
            # 上下文也写不进去——conversation_contexts 要 conversation_id。
            if not platform_owns_persist and getattr(payload, "channel", None) != "xianyu":
                try:
                    await asyncio.to_thread(
                        persist_to_mysql,
                        session_id, user_id, payload.query, cached_answer,
                        cache_contexts, cache_scores, None, None,
                        {
                            "primary": intent_for_cache,
                            "method": "cache_hit",
                            "confidence": 1.0,
                            "intents": [],
                            "entities": cached_entities,
                        },
                    )
                except Exception as e:
                    logger.warning(f"cache 命中后 MySQL 落库失败: {e}")
            # 缓存命中也算一轮：不写上下文的话，同一句问两遍时第一遍的订单号
            # 就丢了（实测：换个新会话重问「ORD… 到哪了」命中缓存 → 上下文空行）
            if settings.ENABLE_CONTEXT_STORE:
                try:
                    await asyncio.to_thread(
                        record_turn_context,
                        session_id,
                        user_id,
                        payload.query,
                        history,
                        {"primary": intent_for_cache, "entities": cached_entities},
                        payload.order_no,
                    )
                except Exception as e:
                    logger.warning(f"cache_hit 上下文写入失败（放行）: {e}")
            # L3：必须与上面的 L2 写入**串行**。两件事各自 to_thread 会并发，
            # 实测 L3 读到 L2 写入前的旧 size，改口因此丢失（BUGS 50）。
            # done 已经发完，这里同步执行几十毫秒不影响用户观感。
            if settings.ENABLE_LONG_TERM_MEMORY:
                capture_turn(user_id, session_id, payload.query, None)
            try_log_action(
                user=user, action="chat_cache_hit", target_type="session",
                target_id=session_id, ip=ip, user_agent=ua,
                detail={"query_len": len(payload.query), "answer_len": len(cached_answer)},
            )
            logger.info(f"/chat cache hit: session={session_id[:12]}... {user_ctx}")
            agent_trace.mark("cache_hit", True)
            agent_trace.mark("intent_method", "cache_hit")
            agent_trace.mark_answer(cached_answer)
            agent_trace.flush("cache_hit")
            return

        full_answer = ""
        # 句级答案自检：按句缓冲，判不过只替换那一句；前端仍收原来的 token 流
        from app.services.chat.answer_verifier import SentenceVerifier
        from app.core.config import settings as _verify_cfg
        # 回答分段：连发合并后的长回答按句末切成最多 3 段（不在句中切）
        from app.services.answer_segmenter import AnswerSegmenter
        segmenter = AnswerSegmenter()
        verifier = None
        contexts: list = []
        scores: list = []
        intent_snap: Optional[dict] = None  # (2.3)：run_stream 内部 _intent 事件的快照
        last_heartbeat = time.time()
        chat_start = time.perf_counter()  # M8：记录 chat 总耗时
 # SSE Resume：本回合 stream_id（前端拿到后用于 resume）
        # + seq 计数器（每个 SSE event 自增，写入 id: 行）
        stream_id = uuid.uuid4().hex[:12]
        seq = 0
        # 把 Synthesizer.run_stream 包装成 async iterator
        # （它原本是同步 generator，用 to_thread 异步化）
        from app.services.chat.orchestrator import Synthesizer as _S
        # 把 sku/order_no context 传给 synthesizer（让 LLM 知道当前商品/订单）
        # 传 session_id 让 OrderContextResolver 加载会话上下文
        sync_iter = iter(_S.run_stream(
            payload.query, user_id, history,
            sku=payload.sku, order_no=payload.order_no,
            session_id=session_id,
            # （2.2）：透传预分类结果，消除同请求双分类（此前规则 miss 时
            # chat.py 与 orchestrator 各调一次 LLM classify，且输入版本不同）
            pre_intent=pre_intent,
            # 会话入口预加载的用户上下文（最近 5 单 + 画像）
            user_context_block=user_context_block,
            # 透传 daemon/caller 已拼好的渠道 context（BuyerProfile + ItemPrompt）
            # 优先级最高——orchestrator 内部不再二次拼装。caller 没传则为空串，
            # orchestrator 退化为 M9.5/V15 内部拼装（向后兼容）
            context_block=getattr(payload, "context_block", None) or "",
            #  channel + buyer_unb 透传，orchestrator 内 refund_query
            # 分支据此路由（channel=xianyu 走闲鱼版 XianyuRefundHandler，
            # 避免被商城 OrderService 的登录检查拦截）
            channel=getattr(payload, "channel", None),
            buyer_unb=getattr(payload, "buyer_unb", None),
            # 全量 system prompt（xianyu 快速通道：跳过 rewrite/RAG 直出 LLM）
            system_prompt=getattr(payload, "system_prompt", None),
        ))

        try:
            while True:
                # 检查客户端是否断开（FastAPI 在 disconnect 时会 raise CancelledError）
                if await request.is_disconnected():
                    logger.info(
                        f"/chat 客户端断开: session={session_id[:12]}..., "
                        f"answer_so_far={len(full_answer)} {user_ctx}"
                    )
                    break

                # heartbeat 节流：每 30s 发一次（在等 LLM token 时穿插）
                now = time.time()
                if now - last_heartbeat >= SSE_HEARTBEAT_INTERVAL:
                    yield _sse_format({"type": "heartbeat", "ts": int(now * 1000)})
                    last_heartbeat = now

                # 拉取下一个事件（用 to_thread 不阻塞事件循环）
                try:
                    item = await asyncio.wait_for(
                        asyncio.to_thread(next, sync_iter, None),
                        timeout=SSE_HEARTBEAT_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    # 等不到下一个 token，发送 heartbeat 后继续等
                    yield _sse_format({"type": "heartbeat", "ts": int(time.time() * 1000)})
                    last_heartbeat = time.time()
                    continue
                except StopIteration:
                    break

                if item is None:  # sentinel: 同步迭代器耗尽
                    break

                event_type, data = item

                if event_type == "_intent":
                    # (2.3)：意图快照内部事件——chat 消费，不外发 SSE
                    intent_snap = data
                    agent_trace.mark_intent(data)
                    continue
                elif event_type == "usage":
                    # 流末帧的 token 消耗，落进 trace 供 D5 成本统计读取
                    agent_trace.mark_usage(data)
                    continue
                elif event_type == "meta":
                    seq += 1
                    meta_payload = {**data, "stream_id": stream_id}
                    # (2.3)：路由质量随 meta 透出（审计候选：method/confidence
                    # 此前只存在于应用日志，前端与观测面不可见）
                    if intent_snap:
                        meta_payload.setdefault("intent_method", intent_snap.get("method"))
                        meta_payload.setdefault("intent_confidence", intent_snap.get("confidence"))
                        if intent_snap.get("upgraded"):
                            meta_payload["intent_upgraded"] = True
                    # 人工接管/升级事件 → degraded 标记
                    if meta_payload.get("intent") == "handoff" or meta_payload.get("handoff"):
                        agent_trace.mark("degraded", True)
                    contexts = meta_payload.get("contexts", [])
                    scores = meta_payload.get("scores", [])
                    # 句级自检依据 = 本轮实际检索到的资料（与模型可见范围一致）
                    if _verify_cfg.ENABLE_ANSWER_VERIFY:
                        verifier = SentenceVerifier(contexts, user_query=payload.query)
                    # 七维评测：检索层快照进 trace（D1 定位依据）
                    agent_trace.mark_retrieval(contexts, scores)
                    yield _sse_format({"type": "meta", **meta_payload}, seq=seq)
                elif event_type == "token":
                    pieces = verifier.feed(data) if verifier is not None else [data]
                    for piece in pieces:
                        if not piece:
                            continue
                        # 分段必须在 full_answer 累加之前完成——落库/缓存/自检
                        # 一律以实际下发文本为准，插入的段间空行也要进 full_answer
                        piece = segmenter.feed(piece)
                        seq += 1
                        # 落库 / 缓存 / checkpoint 一律用「实际下发的文本」，
                        # 不能用原始 token，否则会留下被自检拦掉的错误数字
                        full_answer += piece
                        yield _sse_format({"type": "token", "text": piece}, seq=seq)
 # SSE Resume：异步写 checkpoint（fire-and-forget，
                    # 不阻塞流；CancelledError 块会同步兜底覆盖最终状态）
                    try:
                        asyncio.create_task(asyncio.to_thread(
                            set_stream_checkpoint,
                            session_id, stream_id, full_answer, seq, payload.query,
                        ))
                    except RuntimeError:
                        # 无 running loop（极端边界）→ 跳过；下次 token 会覆盖
                        pass
                elif event_type == "done":
                    # 句级自检收尾：把未成句的残余判完再结束这一轮
                    if verifier is not None:
                        tail = verifier.finish()
                        if tail:
                            seq += 1
                            full_answer += tail
                            yield _sse_format({"type": "token", "text": tail}, seq=seq)
                        # 拦句策略是「丢弃该句」而不是「换成兜底话术」——只有剩下的内容
                        # 几乎为零（核心结论被拦）时才认输，补一句兜底，避免前后矛盾。
                        if verifier.need_fallback(full_answer):
                            from app.services.chat.answer_verifier import (
                                FALLBACK_SENTENCE as _FB,
                            )
                            seq += 1
                            full_answer += _FB
                            agent_trace.mark("fallback_sentence_injected", True)
                            yield _sse_format({"type": "token", "text": _FB}, seq=seq)
                        if verifier.flagged:
                            logger.warning(
                                f"answer verify flagged: reasons={verifier.reasons[:3]} "
                                f"dropped={[d['sentence'][:40] for d in verifier.dropped[:3]]} "
                                f"session={session_id[:12]}..."
                            )
                            agent_trace.mark("answer_verify_flagged", True)
                            # 被替换的原句进 trace：判断「误杀 vs 拦住幻觉」的唯一证据
                            if verifier.dropped:
                                agent_trace.mark("answer_dropped", verifier.dropped[:5])
                            try:
                                badcase_service.enqueue_guard_blocked_safe(
                                    query=payload.query, session_id=session_id,
                                    user_id=user_id, layer="answer_verify",
                                    reason=",".join(verifier.reasons[:3]),
                                )
                            except Exception:
                                pass
                    # 空回复保险丝：本轮若一个可见字符都没发出（未知分支、
                    # 静默拦截、上游返回空串），在出口强制补一句承接话术。
                    # 设计要点：只检查"出口有没有东西"，不依赖任何业务分支——
                    # 这样才能保证以后新增的任何路径都不会出现"发了消息没回应"。
                    # 不靠枚举场景（永远会漏），靠出口断言（漏不掉）。
                    if _verify_cfg.ENABLE_EMPTY_ANSWER_GUARD and not full_answer.strip():
                        # 话术要求：不能让用户重发一遍（那等于把问题退回去）。
                        # 给一个马上能执行的出口：「转人工」下一轮真的会命中 handoff 分支。
                        _EMPTY_FALLBACK = (
                            "这条我收到了。这边一时没接住，你说一句「转人工」，"
                            "我马上让同事接手，不用再重复一遍。"
                        )
                        seq += 1
                        full_answer = _EMPTY_FALLBACK
                        agent_trace.mark("empty_answer_injected", True)
                        logger.warning(
                            f"/chat 空回复已注入兜底: session={session_id[:12]}... "
                            f"query={payload.query[:40]!r}"
                        )
                        yield _sse_format(
                            {"type": "token", "text": _EMPTY_FALLBACK}, seq=seq
                        )
                    # （1.2）：流中途断连的"残答"识别——内容照常落库保留真实记录，
                    # 但禁止写入响应缓存（半截答案进语义缓存会被 0.95 相似命中反复供应，
                    # 把一次网络抖动放大成持续性故障）；SSE done 与审计均带 truncated 标记
                    truncated = bool(isinstance(data, dict) and data.get("truncated"))
                    # write-through（best-effort）
                    # xianyu 渠道跳过——消息归 daemon ConversationRecorder
                    # 实时落库（买家消息到达即落，含接管期），避免双写
                    if getattr(payload, "channel", None) == "xianyu":
                        logger.debug("xianyu 渠道写穿透跳过（daemon recorder 负责）")
                    else:
                        try:
                            await asyncio.to_thread(
                                append_exchange, session_id, payload.query, full_answer
                            )
                        except Exception as e:
                            logger.warning(
                                f"Redis 写穿透失败: session={session_id[:12]}..., {e}"
                            )
                    # 响应缓存（10min 内同 query 不调 LLM）；残答禁入（ 1.2）
                    # 降级兜底也禁入：一次 LLM 抖动/欠费会产出"系统繁忙"，若被缓存，
                    # 同一个问题 10 分钟内所有人（含修好之后）都只能看到这句——
                    # 实测踩过：换回可用模型后同样问法仍返回"系统繁忙"，就是缓存里的旧兜底
                    # 多轮也禁入：缓存 key 不含会话状态，写进去就会被下一轮复用（答反）
                    _degraded = "系统繁忙" in (full_answer or "")
                    if (
                        not truncated
                        and not _degraded
                        and can_use_cache(history)
                        and not guard_degraded
                    ):
                        try:
                            await asyncio.to_thread(
                                put_cached_answer,
                                payload.query,
                                user_id,
                                full_answer,
                                _cache_scope,
                                contexts,
                                scores,
                            )
                        except Exception as e:
                            logger.warning(f"缓存写入失败: {e}")
                    else:
                        logger.warning(
                            f"residual answer cache skipped: session={session_id[:12]}"
                        )
                    if platform_owns_persist:
                        # 平台层（Java 网关）在 done 帧统一落库并回填消息 id
                        assistant_msg_id = None
                    elif getattr(payload, "channel", None) == "xianyu":
                        assistant_msg_id = None  # : daemon recorder 负责
                    else:
                        try:
                            assistant_msg_id = await asyncio.to_thread(
                                persist_to_mysql,
                                session_id, user_id, payload.query, full_answer, contexts, scores,
                                None, None, intent_snap,
                            )
                        except Exception as e:
                            assistant_msg_id = None
                            logger.warning(
                                f"MySQL 写穿透失败: session={session_id[:12]}..., {e}"
                            )
                    # 会话上下文写侧（best-effort，不阻塞 done）：把这一轮观察到的
                    # 事实落进 conversation_contexts，供下一轮「那能退吗」直接复用，
                    # 不再靠 20 条热历史里侥幸还留着订单号。
                    # 必须放在落库之后：上下文通过 conversation_id 关联会话行。
                    if settings.ENABLE_CONTEXT_STORE:
                        try:
                            await asyncio.to_thread(
                                record_turn_context,
                                session_id,
                                user_id,
                                payload.query,
                                history,
                                intent_snap,
                                payload.order_no,
                            )
                        except Exception as e:
                            logger.warning(
                                f"上下文写入失败（放行）: session={session_id[:12]}..., {e}"
                            )
                    # L3 跨会话记忆：只提交候选（跨两个会话确认后才生效），
                    # 写入闸自己会过滤 eval/smoke 流量，这里不必重复判。
                    # 同步调用：必须在 L2 写入完成后再读它的 facts（见缓存分支注释）
                    if settings.ENABLE_LONG_TERM_MEMORY:
                        capture_turn(user_id, session_id, payload.query, None)
                    # P2 长程记忆：每轮 done 后 best-effort 累加 interaction_count；
                    # 若 payload.sku 非空（用户从商品详情跳转），追加到 frequent_skus。
                    # 灰度开关：ENABLE_USER_PROFILE=False 时短路（不调 profile_service）
                    if settings.ENABLE_USER_PROFILE and user_id and user_id != ANONYMOUS_USER_ID:
                        try:
                            await asyncio.to_thread(
                                profile_service.increment_interaction, user_id, 1
                            )
                            if payload.sku:
                                await asyncio.to_thread(
                                    profile_service.append_frequent_skus,
                                    user_id,
                                    [payload.sku],
                                )
                        except Exception as e:
                            # profile 更新失败不影响 done 响应（best-effort）
                            logger.warning(
                                f"profile 更新失败（放行）: user_id={user_id}, {e}"
                            )
                    try_log_action(
                        user=user, action="chat", target_type="session",
                        target_id=session_id, ip=ip, user_agent=ua,
                        detail={
                            "query_len": len(payload.query),
                            "answer_len": len(full_answer),
                            "hits": len(contexts),
                            "stream": True,
                            "truncated": truncated,
                        },
                    )

                    done_payload = {"type": "done", "session_id": session_id}
                    if truncated:
                        done_payload["truncated"] = True
                    # (2.3)：带 assistant 消息行 id——前端按"条"评价
                    # （此前 message_id 恒 0 全退化为会话粒度，👎 反查不到具体回答）
                    if assistant_msg_id:
                        done_payload["assistant_message_id"] = assistant_msg_id
                    yield _sse_format(done_payload, seq=seq + 1)
                    seq += 1
 # SSE Resume：正常完成清理 checkpoint
                    try:
                        await asyncio.to_thread(
                            del_stream_checkpoint, session_id, stream_id
                        )
                    except Exception as e:
                        logger.warning(f"checkpoint 清理失败（TTL 自然过期兜底）: {e}")
                    # 记录 chat 延迟（流式总耗时）
                    metrics.record_chat_latency(
                        round((time.perf_counter() - chat_start) * 1000, 1)
                    )
                    # 七维评测：引用来源进 trace（D2 证据引用定位依据）
                    agent_trace.mark_citations(contexts)
                    # 答案正文进 trace：badcase 回放要对当时原文，不能靠重发复现
                    agent_trace.mark_answer(full_answer)
                    agent_trace.flush("ok")
                    logger.info(
                        f"/chat stream done: session={session_id[:12]}..., "
                        f"answer_len={len(full_answer)}, hits={len(contexts)} {user_ctx}"
                    )

            # 4. graceful close（M7）
            yield _sse_format({"type": "closed"})

        except asyncio.CancelledError:
            # 客户端主动断开（FastAPI 内部触发）
            logger.info(
                f"/chat 客户端取消（CancelledError）: "
                f"session={session_id[:12]}..., answer_so_far={len(full_answer)} {user_ctx}"
            )
 # SSE Resume：同步写最终 checkpoint（兜底覆盖异步 task 可能丢失的写入）
            # 同步调用，不 await，避免在 CancelledError 路径再次被取消
            try:
                set_stream_checkpoint(
                    session_id, stream_id, full_answer, seq, payload.query
                )
            except Exception as e:
                logger.warning(f"checkpoint 同步落盘失败: {e}")
            # 不再 yield（连接已断，yield 无效）
            # 半截答案也留档：outcome=cancelled 时 answer 就是用户实际看到的残缺内容
            agent_trace.mark_answer(full_answer)
            agent_trace.flush("cancelled")
            raise
        except ValueError as e:
            error_msg = str(e)
            logger.error(f"/chat stream 参数错误: {error_msg} {user_ctx}")
            agent_trace.flush("error", error_type="ValueError")
            yield _sse_format({"type": "error", "message": error_msg})
            yield _sse_format({"type": "closed"})
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)[:200]}"
            logger.exception(
                f"/chat stream 调用失败: session={session_id[:12]}... {user_ctx}"
            )
            agent_trace.flush("error", error_type=type(e).__name__)
            yield _sse_format({"type": "error", "message": error_msg})
            yield _sse_format({"type": "closed"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
    # ContextVar 是 per-task，generator 跑在同一 task 内无需 reset
    # （下一个请求会覆盖 set）


# =============================================================
# POST /chat/resume - SSE 流式中断续传（SSE Resume）
# =============================================================
# 设计：
#   - 客户端在 /chat 流中断（未收到 done）后调用
#   - 后端从 Redis checkpoint 读取 prefix_text，重发到前端
#   - 不调 LLM 续写（用户拍板：checkpoint 重发即可，不强求"补全"）
#   - 同 (session_id, stream_id) 最多 resume STREAM_MAX_RESUME_TIMES 次
#   - 第 3 次断流 → 前端走普通重试（catch 后重新调 /chat）
@router.post(
    "/chat/resume",
    summary="SSE 流式中断续传（checkpoint 重发）",
    description=(
        "当 /chat 流中断且未收到 done 时调用。"
        "后端从 Redis checkpoint 读取 prefix_text 一次性重发，"
        "前端可立即渲染（用户视角无感）。"
        "同 (session_id, stream_id) 最多 2 次续传。"
    ),
    response_class=StreamingResponse,
)
async def chat_resume(
    request: Request,
    payload: ResumeRequest,
    user: Optional[User] = Depends(get_current_user_optional),
):
    """SSE 流式中断续传端点

    事件流：
        id: 1
        data: {"type":"resume_prefix","prefix_text":"...","from_event_id":42}\\n\\n

        id: 2
        data: {"type":"done","session_id":"..."}\\n\\n

        id: 3
        data: {"type":"closed"}\\n\\n

    异常路径（前置校验失败，返 410 / 404）：
        - checkpoint 不存在 / TTL 过期 → 410 Gone
        - query 不匹配 → 410 Gone（防 query mismatch 注入）
        - resume 次数超限 → 410 Gone
    """
    session_id = payload.session_id
    stream_id = payload.stream_id
    # 续传若需重新生成，仍按原商品款号绑定切片
    from app.services.policy_service import set_kb_model
    set_kb_model(getattr(payload, "model", None))
    user_id = user.id if user else ANONYMOUS_USER_ID
    user_ctx = (
        f"user={user.username}(id={user.id})" if user else "user=anonymous"
    )

    # 1. 读 checkpoint（不存在或 TTL 过期 → 410）
    cp = await asyncio.to_thread(
        get_stream_checkpoint, session_id, stream_id
    )
    if not cp:
        logger.info(
            f"/chat/resume checkpoint miss: session={session_id[:12]}..., "
            f"stream_id={stream_id} {user_ctx}"
        )
        raise HTTPException(
            status_code=410,
            detail="checkpoint not found or expired",
        )

    # 2. query 校验（防 query mismatch 注入；用户拍板不缓存 query 无关前缀）
    if cp["query"] != payload.query:
        logger.warning(
            f"/chat/resume query mismatch: session={session_id[:12]}..., "
            f"stream_id={stream_id} {user_ctx}"
        )
        raise HTTPException(
            status_code=410,
            detail="query mismatch (resume 必须传相同的 query)",
        )

    # 3. 限流：先 GET 计数，超限直接 410（避免误扣）
    #    race window 极小（< 1ms），业务上可接受
    from app.clients.redis_client import get_client as _redis_get
    count_key = f"chat:stream:resume_count:{session_id}:{stream_id}"
    current_count_raw = await asyncio.to_thread(_redis_get().get, count_key)
    current_count = int(current_count_raw or 0)
    if current_count >= STREAM_MAX_RESUME_TIMES:
        logger.info(
            f"/chat/resume 限流: session={session_id[:12]}..., "
            f"stream_id={stream_id}, count={current_count} {user_ctx}"
        )
        raise HTTPException(
            status_code=410,
            detail=f"resume limit exceeded (max={STREAM_MAX_RESUME_TIMES})",
        )

    # 4. INCR 增加计数（在 GET 通过后；race 时最多多扣 1 次，可接受）
    await asyncio.to_thread(
        increment_resume_count, session_id, stream_id
    )

    logger.info(
        f"/chat/resume start: session={session_id[:12]}..., "
        f"stream_id={stream_id}, prefix_len={len(cp['prefix_text'])}, "
        f"count_after={current_count + 1} {user_ctx}"
    )

    # 5. ContextVar 注入（与 chat 端点对齐，便于日志追踪）
    set_session_id(session_id)
    set_user_id(user_id if user else None)

    # 6. async generator：resume_prefix → done → closed
    async def resume_event_generator():
        seq = 0
        try:
            # 客户端断开检测（resume 流极短，但保留健壮性）
            if await request.is_disconnected():
                logger.info(
                    f"/chat/resume 客户端提前断开: session={session_id[:12]}..., "
                    f"stream_id={stream_id} {user_ctx}"
                )
                return

            # 6.1 resume_prefix：一次性把已流 prefix 重发给前端
            seq += 1
            yield _sse_format(
                {
                    "type": "resume_prefix",
                    "prefix_text": cp["prefix_text"],
                    "from_event_id": int(cp["last_event_id"]),
                    "stream_id": stream_id,
                },
                seq=seq,
            )

            # 6.2 done（标记本回合完成）
            seq += 1
            yield _sse_format(
                {"type": "done", "session_id": session_id},
                seq=seq,
            )

            # 6.3 graceful close
            seq += 1
            yield _sse_format({"type": "closed"}, seq=seq)

            logger.info(
                f"/chat/resume done: session={session_id[:12]}..., "
                f"stream_id={stream_id}, prefix_len={len(cp['prefix_text'])} {user_ctx}"
            )

        except asyncio.CancelledError:
            # resume 流极短，CancelledError 概率极低；保留防御性日志
            logger.info(
                f"/chat/resume CancelledError: session={session_id[:12]}..., "
                f"stream_id={stream_id} {user_ctx}"
            )
            raise

    return StreamingResponse(
        resume_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

# ============================================================
# 闲鱼渠道快速分支（不走通用 RAG）
# ============================================================
async def _xianyu_channel_chat(
    payload,
    session_id: str,
    user_id: int,
    user_ctx: str,
    ip: Optional[str],
    ua: Optional[str],
):
    """闲鱼 IM 渠道：直接调 XianyuRefundHandler / XianyuScenarioRouter

    设计：
    - 退款关键词 → XianyuRefundHandler 走 DB 决策（已退 → 拒；未退 → 转人工）
    - 其他 → 简短回复（不在智能客服通用 RAG 路径上跑，避免"用户未登录"模板）
    - 返 SSE 格式
    """
    from app.services.xianyu.refund_handler import XianyuRefundHandler, RefundDecision
    from app.services.xianyu.scenario_router import (
        XianyuScenarioRouter, Scenario, ScenarioResult,
    )
    from app.services.xianyu.buyer_profile import BuyerProfile, BuyerProfileData

    query = payload.query or ""
    buyer_unb = payload.buyer_unb

    async def _event_gen():
        # 全链路日志：L5 SSE 事件计数（结尾一次性汇总，不在每个 token yield 打）
        sse_counts: dict[str, int] = {}
        try:
            # 1. 退款检测
            refund_keywords = ["退款", "退钱", "退货", "退一下"]
            is_refund = any(kw in query for kw in refund_keywords)

            # 2. 调 XianyuRefundHandler
            db_session_factory = None
            try:
                from app.clients.mysql_client import get_db
                # 简化的 DB session 注入
                from app.core.config import settings
                from sqlalchemy import create_engine
                from sqlalchemy.orm import sessionmaker
                engine = create_engine(settings.DATABASE_URL)
                SessionLocal = sessionmaker(bind=engine)
                db_session = SessionLocal()
            except Exception as e:
                logger.warning(f"/chat xianyu 渠道 DB 连接失败: {e}")
                db_session = None

            if is_refund and db_session is not None:
                try:
                    handler = XianyuRefundHandler(
                        db=db_session, seller_unb=user_ctx or "s1"
                    )
                    decision: RefundDecision = handler.handle_refund_request(
                        unb=buyer_unb, buyer_nick=""
                    )
                    answer = decision.reply_text
                    logger.info(
                        f"/chat xianyu 退款决策 cid={session_id[:12]} unb={buyer_unb} "
                        f"should_reject={decision.should_reject} "
                        f"order_state={decision.order_state}"
                    )
                except Exception as e:
                    logger.warning(f"/chat xianyu RefundHandler 失败: {e}")
                    answer = _xianyu_fallback_text(query)
            else:
                # 3. 非退款 → 调 LLM 简短回复（用 xianyu 场景 prompt）
                answer = await _xianyu_llm_reply(query, buyer_unb, user_ctx)

            # 4. 返 SSE（每次 yield 计数，不打日志 — 末尾汇总）
            sse_counts["meta"] = sse_counts.get("meta", 0) + 1
            yield _sse_format({
                "type": "meta",
                "intent": "xianyu_channel",
                "channel": "xianyu",
            })
            # 分块 token
            chunks = [answer[i:i+10] for i in range(0, len(answer), 10)]
            sse_counts["token"] = sse_counts.get("token", 0) + len(chunks)
            for chunk in chunks:
                yield _sse_format({"type": "token", "text": chunk})
            sse_counts["done"] = sse_counts.get("done", 0) + 1
            yield _sse_format({
                "type": "done",
                "session_id": session_id,
                "assistant_message_id": 0,
            })
            sse_counts["closed"] = sse_counts.get("closed", 0) + 1
            yield _sse_format({"type": "closed"})
            # 全链路日志：L5 SSE_SUMMARY（结尾一次性汇总）
            try:
                from app.core.runtime_log import log_event as _relog
                total = sum(sse_counts.values())
                _relog(
                    "SSE_SUMMARY", "INFO", str(session_id or "-"), str(buyer_unb or "-"),
                    f"xianyu fast-path total_events={total} by_type={sse_counts}",
                    total_events=total,
                    meta_count=sse_counts.get("meta", 0),
                    token_count=sse_counts.get("token", 0),
                    done_count=sse_counts.get("done", 0),
                    closed_count=sse_counts.get("closed", 0),
                    error_count=sse_counts.get("error", 0),
                    channel="xianyu_fast_path",
                )
            except Exception:
                pass
        except Exception as e:
            logger.exception(f"/chat xianyu 渠道异常: {e}")
            yield _sse_format({"type": "error", "message": str(e)[:200]})
            yield _sse_format({"type": "closed"})
            # 全链路日志：L5 SSE_SUMMARY（异常路径也汇总）
            try:
                from app.core.runtime_log import log_event as _relog
                total = sum(sse_counts.values()) + 2  # +error +closed
                _relog(
                    "SSE_SUMMARY", "WARNING", str(session_id or "-"), str(buyer_unb or "-"),
                    f"xianyu fast-path ERROR total_events={total} error={type(e).__name__}",
                    total_events=total,
                    error_count=sse_counts.get("error", 0) + 1,
                    by_type=sse_counts,
                    error=f"{type(e).__name__}: {str(e)[:200]}",
                    channel="xianyu_fast_path",
                )
            except Exception:
                pass

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _xianyu_llm_reply(query: str, buyer_unb: str, user_ctx: str) -> str:
    """闲鱼渠道非退款场景 → 调 LLM 简短回复"""
    from app.core.providers.llm import get_llm_provider

    system = (
        "你是闲鱼卖家「猫与小鼠闲置号」的智能客服。"
        "回复真实、简洁（≤80 字），不要套话。"
        "如果买家问商品细节，结合本店服饰样衣与全新闲置商品回答。"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": query or "在吗"},
    ]
    # 全链路日志：LLM_PROMPT + LLM_RESPONSE（闲鱼渠道不走通用 RAG stream_llm）
    try:
        from app.core.runtime_log import log_event as _relog
        _relog(
            "LLM_PROMPT", "INFO", "-", str(buyer_unb or "-"),
            f"xianyu fast-path system_len={len(system)} user_len={len(query or '')}",
            channel="xianyu_fast_path",
            system_prompt=system[:3000] + ("...(truncated)" if len(system) > 3000 else ""),
            user_message=query or "在吗",
        )
    except Exception:
        pass
    try:
        provider = get_llm_provider()
        result = provider.chat(messages=messages, temperature=0.7)
        answer = (result.get("reply") or "您好，我在这儿。有什么可以帮您的？")[:200]
        try:
            from app.core.runtime_log import log_event as _relog
            _relog(
                "LLM_RESPONSE", "INFO", "-", str(buyer_unb or "-"),
                f"xianyu fast-path answer_len={len(answer)}",
                channel="xianyu_fast_path",
                answer_len=len(answer),
                final_answer=answer[:500],
            )
        except Exception:
            pass
        return answer
    except Exception as e:
        logger.warning(f"/chat xianyu LLM 失败: {e}")
        try:
            from app.core.runtime_log import log_event as _relog
            _relog(
                "LLM_RESPONSE", "ERROR", "-", str(buyer_unb or "-"),
                f"xianyu fast-path LLM failed: {type(e).__name__}",
                channel="xianyu_fast_path",
                error=f"{type(e).__name__}: {str(e)[:200]}",
            )
        except Exception:
            pass
        return _xianyu_fallback_text(query)


def _xianyu_fallback_text(query: str) -> str:
    """LLM 不可用时的兜底回复"""
    q = (query or "").strip()
    if "退款" in q or "退钱" in q:
        return "已为您转接专员，24 小时内会有工作人员与您联系。"
    if "在吗" in q or "你好" in q:
        return "您好，我在这儿。请问有什么可以帮您的？"
    return "您好，我在这儿。请告诉我您想咨询什么？"


# ============================================================
# 闲鱼渠道：注入 BuyerProfile + ItemPrompt 到 context_block
# ============================================================
def _xianyu_inject_context(payload) -> None:
    """闲鱼渠道前置注入（不绕开原 RAG 路径）

    收尾（关键变更）：
    - caller (daemon) 已传 payload.context_block → **不覆盖**直接返回
      （daemon 拼的是 reply_scheduler 上下文：买家昵称/客户类型/沟通偏好 等；
       chat 自动拼的是 XianyuItemRepository 商品话术——可能与 daemon 已拼的
       内容不同时被错误覆盖，bug fix）
    - 否则（前端 / 老 daemon / 没 caller）→ 兜底查 XianyuBuyerProfileV2 +
      XianyuItemRepository 拼 prefix

    流程（仅在 caller 未传时执行）：
    1. 查 XianyuBuyerProfileV2（7 天 TTL 缓存）
    2. 查 XianyuItemRepository（按 item_id 取商品话术）
    3. 拼 prefix 注入 payload.context_block
    """
    # 守护：caller (daemon) 已传 context_block 时跳过覆盖
    # chat.py 紧随其后会把 payload.context_block 透传到 Synthesizer.run_stream，
    # 由 orchestrator 在 system prompt 头部原样使用。
    existing = getattr(payload, "context_block", None)
    if existing:
        logger.info(
            f"xianyu inject 跳过：caller 已传 context_block "
            f"（len={len(existing)}，chat 端不再覆盖）"
        )
        return
    try:
        from app.services.xianyu.xianyu_buyer_profile_v2 import XianyuBuyerProfileV2
        from app.services.xianyu.xianyu_item_repository import XianyuItemRepository
        from app.clients.mysql_client import get_db
        from app.core.config import settings
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(settings.DATABASE_URL)
        Session = sessionmaker(bind=engine)
        db = Session()
        try:
            profile = XianyuBuyerProfileV2(db=db)
            item_repo = XianyuItemRepository(db=db)

            # 1. 买家画像摘要
            profile_ctx = profile.build_context(
                unb=payload.buyer_unb, cid=payload.session_id or "default",
                current_query=payload.query or "",
            )
            nick = profile_ctx.get("nick", "")
            cust_type = profile_ctx.get("customer_type", "")
            proj_cat = profile_ctx.get("project_category", "")
            comm_pref = profile_ctx.get("communication_pref", "")
            has_order = profile_ctx.get("has_order", False)

            # 2. 商品话术（按 item_id 路由）
            item_prompt = ""
            if payload.order_no:  # 闲鱼里 order_no 字段复用为 item_id
                item_prompt = item_repo.get_item_prompt(payload.order_no)
                item_title = item_repo.get_item_title(payload.order_no)
            else:
                item_title = ""

            # 3. 拼 prefix 注入 payload.context_block
            prefix_parts = [
                "【闲鱼 IM 渠道上下文】",
                f"买家昵称：{nick}" if nick else "",
                f"客户类型：{cust_type}" if cust_type else "",
                f"项目类目：{proj_cat}" if proj_cat else "",
                f"沟通偏好：{comm_pref}" if comm_pref else "",
                f"当前商品：{item_title}" if item_title else "",
                "【商品话术】" if item_prompt else "",
                item_prompt,
                f"订单状态：{'有接单记录' if has_order else '未查到接单记录'}",
                "【回复原则】专业、不议价、不催促；引导买家提供图纸/3D 后报价；接单类问题直接转人工。",
            ]
            prefix = "\n".join(p for p in prefix_parts if p)

            # 注入到 payload.context_block 前缀
            if not hasattr(payload, "context_block") or payload.context_block is None:
                payload.context_block = ""
            payload.context_block = (prefix + "\n" + payload.context_block).strip()
            logger.info(
                f"xianyu inject: unb={payload.buyer_unb} item={payload.order_no} "
                f"has_order={has_order} item_prompt_len={len(item_prompt)}"
            )
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"_xianyu_inject_context 失败: {e}")
