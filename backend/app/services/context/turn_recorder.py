"""每轮结束后记录会话事实（写侧编排层）

被 /api/chat 的 done 分支调用，位置在 persist_to_mysql 之后——
因为 conversation_contexts 通过 conversation_id 关联 conversations，
会话行还没落库时写入会被跳过（Java 网关代落库的首轮就是这样）。

三层职责：
- facts.py          纯函数：这一轮在说哪个订单
- 本模块            归属校验 + 调用 service（编排）
- context_service   落库（DB 事务 + 归属校验 + 去重截断）

best-effort：任何异常只 warn，不影响已经发给用户的回答。
"""
from __future__ import annotations

import logging
from typing import Optional

from app.core.config import settings
from app.services.context.context_service import get_context_service
from app.services.context.facts import (
    extract_clothing_facts,
    extract_corrections,
    facts_from_history,
    flatten_facts,
    resolve_turn_order_no,
)
from app.services.session_service import ANONYMOUS_USER_ID
from app.tools.order_tool import OrderTool

logger = logging.getLogger(__name__)


def _order_belongs_to_user(user_id: int, order_no: str) -> bool:
    """归属校验：订单必须属于当前用户，否则不写进上下文。

    为什么要单独查一次：订单号是买家从聊天框里手打的，可能打错、可能贴的是
    别人的单。写进上下文后下一轮会被当成"当前订单"直接用，所以这一道必须在
    写入时做，不能等到读的时候再拦。
    """
    try:
        return OrderTool.get_order_by_no(user_id, order_no) is not None
    except Exception as e:  # noqa: BLE001
        # DB 抖动 → 宁可不写（本轮事实丢失，下一轮 history 还能补）
        logger.warning(f"上下文写入归属校验失败（本轮不写）: order={order_no}, {e}")
        return False


def _collect_clothing_facts(
    session_id: str, user_id: int, query: str, history: Optional[list]
) -> dict:
    """本轮要写入的服装事实（尺码/身高/体重）。

    三层来源，优先级从高到低：
    1. 本轮原话抽取（最可信）
    2. 改口修正（"我记错了，是175" —— 裸数字正常规则抓不到）
    3. 最近几条 user 消息补抽（首轮写入丢失的补偿，只填库里还没有的 key）

    第 3 条限制成"只填空的"：history 里是旧值，用它覆盖库里的新值，
    改口就白做了。
    """
    ctx = get_context_service().load(session_id, user_id)
    known = flatten_facts(ctx.facts)
    hist_facts = facts_from_history(history)

    facts = extract_clothing_facts(query)

    if not facts:
        for key, value in hist_facts.items():
            if key not in known:
                facts[key] = value

    # 改口消歧需要「旧值」。库里没有时（首轮写入还没落库），用历史推算值兜底——
    # 否则「先说170，紧接着说记错了175」在第二轮就会丢：既没有库里的 170，
    # 也抽不到裸数字 175。
    known_for_correction = {
        **{k: v for k, v in hist_facts.items() if k not in known},
        **known,
    }
    facts.update(extract_corrections(query, known_for_correction))
    return facts


def record_turn_context(
    session_id: str,
    user_id: int,
    query: str,
    history: Optional[list] = None,
    intent_snap: Optional[dict] = None,
    order_no_param: Optional[str] = None,
) -> bool:
    """记录本轮会话事实（意图 / 当前订单 / 订单集合 / 服装事实）。

    Args:
        session_id: 会话 ID
        user_id: 用户 ID（匿名直接跳过——匿名没有可归属的订单）
        query: 本轮买家原话
        history: 清洗后的历史（含上一轮的订单号与自述事实）
        intent_snap: 当轮意图快照（primary / entities）
        order_no_param: 平台注入的 order_no（用户从 OrderCard 点进来时有值）

    Returns:
        是否真的写了（False 表示跳过，不是错误）
    """
    if not settings.ENABLE_CONTEXT_STORE:
        return False
    if not session_id or not user_id or user_id == ANONYMOUS_USER_ID:
        return False

    try:
        snap = intent_snap or {}
        order_no = resolve_turn_order_no(
            history=history,
            query=query,
            entities=snap.get("entities") or {},
            order_no=order_no_param,
        )
        owned = bool(order_no) and _order_belongs_to_user(user_id, order_no)
        if order_no and not owned:
            logger.info(
                f"上下文跳过订单号（归属校验未通过）: order={order_no}, "
                f"user_id={user_id}, session={session_id[:12]}..."
            )

        clothing = _collect_clothing_facts(session_id, user_id, query, history)

        return get_context_service().record_turn(
            session_id=session_id,
            user_id=user_id,
            last_intent=snap.get("primary"),
            order_no=order_no,
            order_no_owned=owned,
            facts=clothing or None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"上下文记录失败（放行）: session={session_id[:12]}..., user_id={user_id}, {e}"
        )
        return False
