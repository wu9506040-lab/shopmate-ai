"""ContextService - 会话上下文 KV 读写（M14）

按 CLAUDE.md §6 + §9.2.2：services/ 编排层，调 models/conversation_context。
被 chat/orchestrator.py 调用，每轮 /chat 启动期 load，done 后 update。

设计：
- 1:1 → conversations.id（conversation_context.conversation_id 是 PK）
- session_id 是会话公开 ID，需通过 conversations 表反查 conversation.id
- best-effort 写：context DB 异常仅 warning，不影响主流程
- 灰度开关：ENABLE_CONTEXT_STORE=False 时短路返默认上下文（不读不写）

§3.3 YAGNI 边界：
- 不做事件流（context_events），需要时 messages JOIN 即可
- 不做多设备同步，单 session 即可
- 不做 TTL 清理，按 conversations.deleted 跟随
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.clients.mysql_client import with_safe_session
from app.core.config import settings
from app.models.conversation import Conversation
from app.models.conversation_context import ConversationContextRow
from app.services.context.facts import merge_facts, merge_resolved_orders

logger = logging.getLogger(__name__)

# 订单号新鲜度窗口：超过这个时长没更新的上下文，**订单号**不再可信。
# 与 Redis 会话热历史的 30min 空闲过期保持同一口径——两层记忆不能一个还记得、
# 一个已经忘了，否则"订单号还在但原文没了"会出现前后不一致的回答。
#
# 注意作用域：只作用于订单号（会串单：买家早就换了单，旧单号还在）。
# 身高体重这类买家自述事实**不适用**——它们不会因为 40 分钟没说话就变成假的，
# 会话结束（TTL 到期）才是它们的生命周期终点。
#
# 判据写在 SQL 里（TIMESTAMPDIFF(MINUTE, UPDATE_TIME, NOW())），不在 Python 里减：
# 应用进程时区（容器默认 UTC）和 MySQL 会话时区（+08:00）可以不一致，
# 实测 Python now()=13:54 而 MySQL NOW()=21:54，减出来是负数——
# 「永远新鲜」，过期规则形同不存在。谁的时钟谁负责。
CONTEXT_FRESH_MINUTES: int = 30
_IDLE_MINUTES = func.timestampdiff(
    sa_text("MINUTE"), ConversationContextRow.update_time, func.now()
)


def _log_fact_changes(changes: list[dict], session_id: str) -> None:
    """改口日志：INFO 只记 key 与「是否覆盖」，值只在 DEBUG。

    身高体重是买家自述，日志文件不该出现「170cm/60kg」这种原文组合——
    日志会被采集、被转发，一旦落进去就成了新的泄露面。
    """
    for c in changes or []:
        logger.info(
            f"[memory] 事实更新 session={session_id[:12]}... "
            f"key={c.get('key')} overwrote={c.get('from') is not None}"
        )
        logger.debug(f"[memory] 事实值 {c.get('key')}: {c.get('from')} -> {c.get('to')}")


@dataclass
class ConversationContext:
    """会话上下文（运行时实例，不直接序列化）"""

    session_id: str
    user_id: int
    last_intent: Optional[str] = None
    current_order_no: Optional[str] = None
    flow_state: Optional[str] = None
    resolved_orders: list = field(default_factory=list)
    flow_payload: dict = field(default_factory=dict)
    # 买家自述事实（白名单）{"height_cm": {"value": 175, "prev": 170, ...}}
    facts: dict = field(default_factory=dict)
    # 会话入口预加载的最近订单精简版（[{order_no, status, items_text}]）
    # 第一轮加载后缓存，避免每条消息重复查 DB；30min 空闲过期后重查
    recent_orders_summary: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class ContextService:
    """会话上下文服务（DB 读写）"""

    def load(self, session_id: str, user_id: int) -> ConversationContext:
        """加载会话上下文（不存在则返回空 context）。

        灰度开关：ENABLE_CONTEXT_STORE=False 时直接返空 context（短路）。
        DB 异常 best-effort：返空 context（不抛）。
        """
        if not settings.ENABLE_CONTEXT_STORE:
            return ConversationContext(session_id=session_id, user_id=user_id)

        if not session_id or not user_id:
            return ConversationContext(session_id=session_id or "", user_id=user_id or 0)

        try:
            with with_safe_session(commit=False) as db:
                # 1. 反查 conversation.id by session_id
                conv = db.execute(
                    select(Conversation).where(
                        Conversation.session_id == session_id,
                        Conversation.deleted == 0,
                    )
                ).scalar_one_or_none()
                if conv is None:
                    # conversation 行尚未落库（典型：会话开始第一轮）→ 返空 context
                    return ConversationContext(session_id=session_id, user_id=user_id)

                # 2. 查 context 行（顺带取空闲分钟数：DB 时钟判新旧，见 CONTEXT_FRESH_MINUTES）
                hit = db.execute(
                    select(
                        ConversationContextRow,
                        _IDLE_MINUTES.label("idle_minutes"),
                    ).where(
                        ConversationContextRow.conversation_id == conv.id,
                        ConversationContextRow.deleted == 0,
                    )
                ).first()

                if hit is None:
                    return ConversationContext(session_id=session_id, user_id=user_id)

                row, idle_minutes = hit

                # 2.1 归属校验：会话上下文里的订单号是买家私有信息。
                # 只要 session_id 被复用/猜到，不做校验就等于把别人的订单号交给
                # 当前用户（与 BUGS 35 同源的越权面）。不一致 → 当作没有上下文。
                if int(row.user_id or 0) != int(user_id):
                    logger.warning(
                        f"context_service.load 归属不符，已拒绝: session={session_id[:12]}..., "
                        f"row_user={row.user_id}, req_user={user_id}"
                    )
                    return ConversationContext(session_id=session_id, user_id=user_id)

                # 2.2 新鲜度：只裁掉订单类状态，买家自述事实（facts）保留。
                # 为什么分开：订单号过期是因为「买家可能早换了单」，继续用会串单；
                # 身高体重不存在这个问题，跟着会话 TTL 走就行。
                stale = idle_minutes is not None and int(idle_minutes) > CONTEXT_FRESH_MINUTES
                if stale:
                    logger.info(
                        f"context_service.load 订单状态已过期({int(idle_minutes)}min)，"
                        f"facts 保留: session={session_id[:12]}..."
                    )

                return ConversationContext(
                    session_id=session_id,
                    user_id=user_id,
                    last_intent=row.last_intent,
                    current_order_no=None if stale else row.current_order_no,
                    flow_state=row.flow_state,
                    resolved_orders=[] if stale else list(row.resolved_orders or []),
                    flow_payload=dict(row.flow_payload or {}),
                    facts=dict(row.facts or {}),
                )
        except Exception as e:
            logger.warning(
                f"context_service.load failed: session={session_id[:12]}..., "
                f"user_id={user_id}, {e}"
            )
            return ConversationContext(session_id=session_id, user_id=user_id)

    def update(
        self,
        session_id: str,
        user_id: int,
        last_intent: Optional[str] = None,
        current_order_no: Optional[str] = None,
        flow_state: Optional[str] = None,
        resolved_orders: Optional[list] = None,
        flow_payload: Optional[dict] = None,
    ) -> bool:
        """部分更新会话上下文（None 字段不覆盖）。

        灰度开关：ENABLE_CONTEXT_STORE=False 时短路返 True（不写）。
        DB 异常 best-effort：返 False（不抛）。
        """
        if not settings.ENABLE_CONTEXT_STORE:
            return True

        if not session_id or not user_id:
            return False

        try:
            with with_safe_session(commit=True) as db:
                # 1. UPSERT conversation（与现有 session_service 一致：lazy 创建）
                # 这里只读不创建（conversations 由 persist_to_mysql 写穿时创建）；
                # 如果 conversation 还没落库，跳过（让下一轮自然写）
                conv = db.execute(
                    select(Conversation).where(
                        Conversation.session_id == session_id,
                        Conversation.deleted == 0,
                    )
                ).scalar_one_or_none()
                if conv is None:
                    logger.debug(
                        f"context_service.update: conversation 尚未落库, "
                        f"session={session_id[:12]}..., user_id={user_id}（放行）"
                    )
                    return False

                # 2. 读现有行（合并 None 字段用）
                row = db.execute(
                    select(ConversationContextRow).where(
                        ConversationContextRow.conversation_id == conv.id,
                        ConversationContextRow.deleted == 0,
                    )
                ).scalar_one_or_none()

                if row is None:
                    # 不存在 → INSERT
                    new_row = ConversationContextRow(
                        conversation_id=conv.id,
                        user_id=user_id,
                        last_intent=last_intent,
                        current_order_no=current_order_no,
                        flow_state=flow_state,
                        resolved_orders=resolved_orders or [],
                        flow_payload=flow_payload or {},
                    )
                    db.add(new_row)
                else:
                    # 归属校验（同 load）：行属于别人 → 拒绝写入，避免覆盖他人上下文
                    if int(row.user_id or 0) != int(user_id):
                        logger.warning(
                            f"context_service.update 归属不符，已拒绝: "
                            f"session={session_id[:12]}..., row_user={row.user_id}, req_user={user_id}"
                        )
                        return False
                    # 存在 → 部分更新（None 不覆盖）
                    if last_intent is not None:
                        row.last_intent = last_intent
                    if current_order_no is not None:
                        row.current_order_no = current_order_no
                    if flow_state is not None:
                        row.flow_state = flow_state
                    if resolved_orders is not None:
                        row.resolved_orders = resolved_orders
                    if flow_payload is not None:
                        row.flow_payload = flow_payload
                return True
        except Exception as e:
            logger.warning(
                f"context_service.update failed: session={session_id[:12]}..., "
                f"user_id={user_id}, {e}"
            )
            return False

    def record_turn(
        self,
        session_id: str,
        user_id: int,
        last_intent: Optional[str] = None,
        order_no: Optional[str] = None,
        order_no_owned: bool = False,
        facts: Optional[dict] = None,
    ) -> bool:
        """一轮结束后记录会话事实（写侧唯一入口）。

        与 update() 的区别：
        - update() 是「调用方明确知道要写什么」，None 表示"别动这个字段"
        - record_turn() 是「这一轮的观察结果」，语义是**累加**：
          last_intent 每轮覆盖；order_no 只在确认归属时覆盖 current_order_no，
          同时并进 resolved_orders（去重 + 截断）；facts 同名 key 覆盖、异名追加

        order_no 为空时不覆盖 current_order_no——买家第二轮只说"那能退吗"，
        上一轮锁定的单必须留着，这正是这个模块存在的意义。

        Args:
            session_id: 会话公开 ID
            user_id: 当前用户
            last_intent: 本轮意图
            order_no: 本轮判定出的订单号（格式已校验）
            order_no_owned: 订单是否已确认属于该用户（**未确认则不写**，
                否则会把别人的订单号记进本会话，下一轮当成自己的单回答）
            facts: 本轮抽到的服装事实（size/height_cm/weight_kg），改口直接覆盖

        Returns:
            True 写入成功；False 跳过（会话未落库 / 归属不符 / 异常）
        """
        if not settings.ENABLE_CONTEXT_STORE:
            return True
        if not session_id or not user_id:
            return False

        effective_order_no = order_no if (order_no and order_no_owned) else None
        # 本地时间，与 messages.create_time / badcase.create_time 同口径。
        # 不用 UTC：这个字段是给人看的（排查"什么时候改的口"），
        # 记录里出现 14:43 而人看的是 22:43，只会让排查多绕一圈。
        stamp = datetime.datetime.now().isoformat(timespec="seconds")

        try:
            with with_safe_session(commit=True) as db:
                conv = db.execute(
                    select(Conversation).where(
                        Conversation.session_id == session_id,
                        Conversation.deleted == 0,
                    )
                ).scalar_one_or_none()
                if conv is None:
                    # 会话尚未落库（Java 网关代落库的首轮）→ 跳过，下一轮 history
                    # 里还留着订单号，会自然补回来，不需要额外的缓冲层
                    logger.debug(
                        f"context_service.record_turn: conversation 尚未落库, "
                        f"session={session_id[:12]}...（跳过，下一轮补偿）"
                    )
                    return False

                row = db.execute(
                    select(ConversationContextRow).where(
                        ConversationContextRow.conversation_id == conv.id,
                        ConversationContextRow.deleted == 0,
                    )
                ).scalar_one_or_none()

                if row is None:
                    merged_facts, changes = merge_facts(None, facts, updated_at=stamp)
                    _log_fact_changes(changes, session_id)
                    db.add(ConversationContextRow(
                        conversation_id=conv.id,
                        user_id=user_id,
                        last_intent=last_intent,
                        current_order_no=effective_order_no,
                        resolved_orders=merge_resolved_orders(None, effective_order_no),
                        facts=merged_facts or None,
                    ))
                    return True

                if int(row.user_id or 0) != int(user_id):
                    logger.warning(
                        f"context_service.record_turn 归属不符，已拒绝: "
                        f"session={session_id[:12]}..., row_user={row.user_id}, req_user={user_id}"
                    )
                    return False

                if last_intent:
                    row.last_intent = last_intent
                if effective_order_no:
                    row.current_order_no = effective_order_no
                    row.resolved_orders = merge_resolved_orders(
                        row.resolved_orders, effective_order_no
                    )
                if facts:
                    merged_facts, changes = merge_facts(row.facts, facts, updated_at=stamp)
                    row.facts = merged_facts
                    _log_fact_changes(changes, session_id)
                return True
        except Exception as e:
            logger.warning(
                f"context_service.record_turn failed: session={session_id[:12]}..., "
                f"user_id={user_id}, {e}"
            )
            return False


# =============================================================
# 工厂入口（依赖倒置 + 单例）
# =============================================================
_service: Optional[ContextService] = None


def get_context_service() -> ContextService:
    """工厂入口。业务模块**只能**通过此函数获取（禁止直接 new）。"""
    global _service
    if _service is None:
        _service = ContextService()
    return _service


def reset_context_service() -> None:
    """测试钩子：重置单例（仅供 test fixtures）。"""
    global _service
    _service = None