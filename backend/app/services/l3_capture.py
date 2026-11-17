"""L3 写入闸：把本轮的可信事实提交为长期候选

与 L2（turn_recorder）的分工：
- L2 记**本次会话**的事实：订单号、尺码、身高体重，一轮就生效
- L3 记**跨会话**的稳定偏好：只有被两个不同会话确认过的才生效

所以这里不再抽身高体重（会过期、也可能被改口），只提交两类：
- size：来自 L2 已抽出的 size（买家明确说过自己的码）
- fabric_pref：买家明确表达的面料偏好（"我不要羊毛""喜欢纯棉"）

`after_sales_conclusion` 在白名单里，但**没有自动抽取器**：售后结论需要语义判断，
规则抽取容易把"我想退货"当成"历史售后结论"。宁可不写，也不写错。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.core.config import settings
from app.models.session_meta import KIND_BIZ
from app.services import long_term_memory as l3
from app.services.context.context_service import get_context_service
from app.services.session_kind import get_session_kind

logger = logging.getLogger(__name__)

# 只认明确表达的偏好句式 + 具体面料词，不抓"这件什么面料"这类提问
_FABRIC_WORDS = "纯棉|全棉|羊毛|羊绒|桑蚕丝|聚酯|涤纶|真丝|棉|麻|加绒|防水"
_FABRIC_PREF_RE = re.compile(
    # 量词必须非贪婪：否则「喜欢纯棉」会被吞成「棉」，丢掉"纯"字
    rf"(我|本人).{{0,4}}?(喜欢|偏好|不要|要|别|怕|受不了).{{0,3}}?({_FABRIC_WORDS})"
    rf"|(?:我不喜欢|不要|别买|别).{{0,3}}?({_FABRIC_WORDS})"
    rf"|(?:喜欢|偏好).{{0,3}}?({_FABRIC_WORDS})的?"
)


def extract_fabric_pref(query: Optional[str]) -> Optional[str]:
    """抽面料偏好，返回短字符串（不返回整句，防止把对话原文存进长期记忆）。"""
    if not query:
        return None
    m = _FABRIC_PREF_RE.search(query)
    if not m:
        return None
    parts = [g for g in m.groups() if g]
    fabric = parts[-1] if parts else None
    if not fabric:
        return None
    neg = any(w in m.group(0) for w in ("不要", "别", "不喜欢", "怕", "受不了"))
    return ("不要" if neg else "喜欢") + fabric


def capture_turn(
    user_id: int,
    session_id: str,
    query: str,
    l2_facts: Optional[dict] = None,
) -> dict:
    """best-effort 提交本轮候选；返回本次尝试的 key→是否变更（便于日志/评测）。"""
    result: dict = {}
    if not settings.ENABLE_LONG_TERM_MEMORY:
        return result
    if not user_id or user_id <= 0 or not session_id:
        return result

    try:
        kind = get_session_kind(session_id)
        if kind != KIND_BIZ:
            # 评测/排障流量绝不能进长期记忆——否则下次真人来问，
            # 系统会拿脚本里的"我穿 XXL"当真事
            return {}

        if l2_facts is None:
            l2_facts = get_context_service().load(session_id, user_id).facts

        candidates: dict = {}
        size = (l2_facts or {}).get("size")
        if size:
            cell = size.get("value") if isinstance(size, dict) else size
            if cell:
                candidates["size"] = str(cell)

        pref = extract_fabric_pref(query)
        if pref:
            candidates["fabric_pref"] = pref

        for key, value in candidates.items():
            result[key] = l3.offer_fact(user_id, key, value, session_id, kind)
        if result:
            logger.info(f"[L3] 候选已提交: user={user_id} {result}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[L3] 写入闸失败（放行）: user_id={user_id}, {e}")
    return result
