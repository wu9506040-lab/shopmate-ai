"""HandoffTicket 服务 - 人工介入工单的创建/排队/认领/回复结单

调用方：
- EscalationService.handoff() 末尾 persist_handoff()（best-effort，参照 audit_service 模式）
- app/api/admin_handoff.py 坐席工作台四端点

设计：
- 幂等合并：同 session 已有 pending/taken 工单时不新建，更新 summary/priority（防用户连点"转人工"刷屏队列）
- resolve 注入人工会话：Message(role=assistant, contexts={"human_agent_ticket": ...})——
  人工回复进同一条对话时间线，前端按 contexts 标"人工客服"徽标
- 独立 DB session（persist 侧）：异常只 warning，绝不阻断聊天主链路
"""
import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.handoff_ticket import HandoffStatus, HandoffTicket
from app.models.message import Message

logger = logging.getLogger(__name__)

# 优先级排序权重（列表展示用：P0 最前）
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}


def _new_ticket_no() -> str:
    return f"HFT-{uuid.uuid4().hex[:12].upper()}"


# =============================================================
# 创建（best-effort，EscalationService 调用侧无 db 依赖）
# =============================================================

REASON_LABELS = {
    "user_requested": "用户主动要求人工客服",
    "agent_unavailable": "AI 服务异常 / 连续失败，自动兜底转人工",
    "business_rule": "命中业务规则，需人工判定",
    "order_modify": "订单信息修改（发货后改地址/电话），必须人工核实",
    "low_confidence": "AI 回答置信度不足，建议人工确认",
    "no_kb_hit": "知识库未命中相关条款，需人工解答",
    "manual": "坐席手动创建",
}


def build_reason_detail(
    reason: str,
    category: Optional[str] = None,
    matched_keyword: Optional[str] = None,
    payload_dict: Optional[dict] = None,
) -> str:
    """把英文 reason code + 触发依据拼成坐席可读的中文说明。

    为什么单独存一列：工单是给坐席看的，`business_rule` / `user_requested`
    这类 code 和散落在 summary JSON 里的上下文无法快速判断优先级与处理方式。
    这里同时保留「原因 + 触发依据 + 用户原话片段」，随工单一起落库。
    """
    payload = payload_dict or {}
    parts = [REASON_LABELS.get(reason, f"转人工原因：{reason}")]
    if category:
        parts.append(f"分类：{category}")
    if matched_keyword:
        parts.append(f"触发依据：{matched_keyword}")
    query = (
        payload.get("query")
        or payload.get("user_query")
        or payload.get("last_user_message")
        or ""
    )
    if query:
        parts.append(f"用户原话：{str(query)[:80]}")
    failure = payload.get("agent_failure_context") or payload.get("failure_context")
    if failure:
        parts.append(f"异常上下文：{str(failure)[:80]}")
    return "；".join(parts)[:500]


def persist_handoff(
    user_id: int,
    session_id: str,
    reason: str,
    payload_dict: dict,
    category: Optional[str] = None,
    priority: Optional[str] = "P2",
    matched_keyword: Optional[str] = None,
    db: Optional[Session] = None,
) -> Optional[int]:
    """持久化一张转人工工单；同 session 未结单则合并更新。

    Args:
        db: 显式 session（测试/API 内复用）；None 时自开独立 session（audit 模式）
    Returns:
        ticket_id；失败返回 None（不抛，主链路无感）
    """
    own_session = db is None
    _db = db
    reason_detail = build_reason_detail(reason, category, matched_keyword, payload_dict)
    # 同时写进 summary，方便审计追溯（不改原有字段语义）
    payload_with_reason = {**(payload_dict or {}), "handoff_reason": reason_detail}
    try:
        if own_session:
            from app.clients.mysql_client import get_session_local
            _db = get_session_local()()

        existing = _db.execute(
            select(HandoffTicket)
            .where(HandoffTicket.session_id == session_id)
            .where(HandoffTicket.status.in_([HandoffStatus.PENDING.value, HandoffStatus.TAKEN.value]))
            .where(HandoffTicket.deleted == 0)
            .order_by(HandoffTicket.id.desc())
            .limit(1)
        ).scalar_one_or_none() if session_id else None

        if existing is not None:
            # 合并：刷新上下文与优先级（升级词可能把 P2 抬到 P0）
            existing.summary = payload_with_reason
            existing.reason_detail = reason_detail
            if priority == "P0" or (existing.priority or "P2") == "P2":
                existing.priority = priority or existing.priority
            _db.commit()
            return existing.id

        ticket = HandoffTicket(
            ticket_no=_new_ticket_no(),
            session_id=session_id or "",
            user_id=user_id,
            reason=reason,
            reason_detail=reason_detail,
            category=category,
            priority=priority or "P2",
            matched_keyword=matched_keyword,
            status=HandoffStatus.PENDING.value,
            summary=payload_with_reason,
        )
        _db.add(ticket)
        _db.commit()
        _db.refresh(ticket)
        logger.info(
            f"handoff ticket created: no={ticket.ticket_no} user={user_id} "
            f"reason={reason} priority={ticket.priority}",
            extra={"user_id": user_id},
        )
        return ticket.id
    except Exception as e:  # best-effort：绝不阻断聊天
        logger.warning(f"persist_handoff failed (non-blocking): {e}")
        try:
            if _db is not None:
                _db.rollback()
        except Exception:
            pass
        return None
    finally:
        if own_session and _db is not None:
            try:
                _db.close()
            except Exception:
                pass


# =============================================================
# 坐席工作台读写（API 层持 db）
# =============================================================

def create_modify_handoff(
    user_id: int,
    session_id: str,
    order_no: str,
    order_status: str,
    action: str,
    detail: Optional[dict] = None,
) -> dict:
    """订单改址/改电话被状态门禁拦下时建工单（工具层调用，无 db 依赖）。

    为什么单独封一层而不复用 persist_handoff 的返回值：
    persist_handoff 只返 ticket_id，工具层要回给用户「工单号」；
    这里把 id + ticket_no 一起返，避免工具层直连 ORM 查表。

    Returns:
        {"ticket_id": int|None, "ticket_no": str|None, "created": bool}
        建单失败时 created=False（best-effort，不阻断对话）
    """
    payload = {
        "type": "order_modify_blocked",
        "order_no": order_no,
        "order_status": order_status,
        "action": action,
        "detail": detail or {},
    }
    ticket_id = persist_handoff(
        user_id=user_id,
        session_id=session_id or "",
        reason="order_modify",
        payload_dict=payload,
        # category 按既有约定用中文（坐席工作台直接展示，不做映射）；
        # reason 保留英文 code 供机器统计/筛选。
        category="订单修改",
        priority="P1",          # 已发货改址/改电 = 派件风险，P1 不占 P0 队列
        matched_keyword=action,
    )
    if ticket_id is None:
        return {"ticket_id": None, "ticket_no": None, "created": False}
    try:
        from app.clients.mysql_client import get_session_local
        db = get_session_local()()
        try:
            row = db.query(HandoffTicket).filter(HandoffTicket.id == ticket_id).first()
            return {
                "ticket_id": ticket_id,
                "ticket_no": row.ticket_no if row else None,
                "created": True,
            }
        finally:
            db.close()
    except Exception as e:  # 查号失败不影响建单结果
        logger.warning(f"create_modify_handoff 取单号失败: {e}")
        return {"ticket_id": ticket_id, "ticket_no": None, "created": True}


def list_tickets(db: Session, status: Optional[str] = None, page: int = 1, page_size: int = 20) -> dict:
    """按状态排队列（P0 优先），返回 {total, page, items[dict]}"""
    stmt = select(HandoffTicket).where(HandoffTicket.deleted == 0)
    if status:
        stmt = stmt.where(HandoffTicket.status == status)
    rows = db.execute(stmt.order_by(HandoffTicket.id.desc()).limit(500)).scalars().all()
    rows = sorted(rows, key=lambda t: (_PRIORITY_ORDER.get(t.priority, 9), -t.id))
    total = len(rows)
    start = (max(1, page) - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_ticket_brief(t) for t in rows[start:start + page_size]],
    }


def get_ticket_detail(db: Session, ticket_id: int) -> Optional[dict]:
    """工单详情 + 会话最近 20 条消息（坐席看上下文用）"""
    t = db.get(HandoffTicket, ticket_id)
    if t is None or t.deleted:
        return None
    msgs = []
    if t.session_id:
        msgs = db.execute(
            select(Message)
            .where(Message.session_id == t.session_id)
            .where(Message.deleted == 0)
            .order_by(Message.id.desc())
            .limit(20)
        ).scalars().all()
        msgs = list(reversed(msgs))
    detail = _ticket_brief(t)
    detail["summary"] = t.summary
    detail["reply"] = t.reply
    detail["messages"] = [
        {
            "id": m.id, "role": m.role, "content": m.content,
            "create_time": m.create_time.isoformat() if m.create_time else None,
            "is_human_agent": bool(m.contexts and m.contexts.get("human_agent_ticket")),
        }
        for m in msgs
    ]
    return detail


def take_ticket(db: Session, ticket_id: int, admin_username: str) -> dict:
    """认领工单（pending→taken；已被他人认领返回 409 语义）"""
    t = db.get(HandoffTicket, ticket_id)
    if t is None or t.deleted:
        return {"ok": False, "code": 404, "error": "工单不存在"}
    if t.status != HandoffStatus.PENDING.value:
        return {"ok": False, "code": 409, "error": f"工单状态为 {t.status}，已被 {t.assignee or '—'} 处理"}
    t.status = HandoffStatus.TAKEN.value
    t.assignee = admin_username
    db.commit()
    return {"ok": True, "ticket": _ticket_brief(t)}


def resolve_ticket(db: Session, ticket_id: int, admin_username: str, reply: str) -> dict:
    """结单：人工回复注入会话（assistant + human_agent 标记）→ resolved

    注入后用户侧下次拉取会话历史即可看到人工回复（MySQL 冷路径为准）。
    """
    t = db.get(HandoffTicket, ticket_id)
    if t is None or t.deleted:
        return {"ok": False, "code": 404, "error": "工单不存在"}
    if t.status == HandoffStatus.RESOLVED.value:
        return {"ok": False, "code": 409, "error": "工单已结单"}
    if not (reply or "").strip():
        return {"ok": False, "code": 400, "error": "回复内容不能为空"}

    if t.session_id:
        msg = Message(
            session_id=t.session_id,
            user_id=t.user_id,
            role="assistant",
            content=reply.strip()[:2000],
            contexts={"human_agent_ticket": t.ticket_no, "agent_name": admin_username},
        )
        db.add(msg)
    t.status = HandoffStatus.RESOLVED.value
    t.reply = reply.strip()[:2000]
    if not t.assignee:
        t.assignee = admin_username
    db.commit()
    # （1.4）：人工回复只写了 MySQL，而用户拉历史 Redis 热路径优先
    # （session_service.load_history_with_fallback）——热会话未过期时用户
    # 永远看不到坐席回复（M15"回复注入会话"宣称的真实断链）。失效热缓存，
    # 下一次加载走 MySQL 冷回填。失败仅告警（TTL 600s 是最终兜底）。
    if t.session_id:
        try:
            from app.services import redis_store
            redis_store.clear_history(t.session_id)
        except Exception as e:
            logger.warning(
                f"resolve: redis history invalidate failed（热会话暂不可见人工回复）: {e}"
            )
    logger.info(f"handoff ticket resolved: no={t.ticket_no} by={admin_username}")
    return {"ok": True, "ticket": _ticket_brief(t)}


def close_ticket(db: Session, ticket_id: int, admin_username: str) -> dict:
    """关闭（不回复直接归档，如误触发/用户已离开）"""
    t = db.get(HandoffTicket, ticket_id)
    if t is None or t.deleted:
        return {"ok": False, "code": 404, "error": "工单不存在"}
    t.status = HandoffStatus.CLOSED.value
    if not t.assignee:
        t.assignee = admin_username
    db.commit()
    return {"ok": True, "ticket": _ticket_brief(t)}


def _ticket_brief(t: HandoffTicket) -> dict:
    return {
        "id": t.id,
        "ticket_no": t.ticket_no,
        "session_id": t.session_id,
        "user_id": t.user_id,
        "reason": t.reason,
        "category": t.category,
        "priority": t.priority,
        "matched_keyword": t.matched_keyword,
        "status": t.status,
        "assignee": t.assignee,
        "create_time": t.create_time.isoformat() if t.create_time else None,
        "update_time": t.update_time.isoformat() if t.update_time else None,
    }
