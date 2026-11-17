"""
OrderModifyTool - 售中订单修改 Tool（写操作 · 2 步确认 + 状态门禁转人工）

按 CLAUDE.md §9.3.3 + §9.5.1：写操作强制 user_id 防越权，走 Service Protocol 不直连 ORM。

2026-11 规则变更（业务方口径）：
1. **只保留两种写操作**：改收货地址、改联系电话。
   改规格（modify_item_spec）/ 合并订单（merge_orders）不再对 AI 开放，
   已从工具注册表移除（service 层能力保留，如需后台使用另开入口）。
2. **只有未发货（pending/paid）才能自助改**。已发货（shipped，含「已发货未揽收」）、
   已签收、已完成、已退款 → 一律不写，改为**建人工工单转坐席**。

为什么门禁放在「确认之前」：
    先问用户"确认改吗？"再告诉他改不了，是两次打扰；且用户确认后仍被服务层
    拒绝，LLM 容易误报"修改成功"。所以顺序是：查状态 → 不允许就转人工 →
    允许才进确认流程 → 票据校验 → 真正写。

三种返回形态（LLM 与前端都能识别）：
- {"status": "needs_confirmation", "prompt": ..., "ticket": ...}  待用户确认
- {"status": "needs_handoff", "ticket_no": ...}                  已转人工（不可自助改）
- ModifyResult.model_dump()                                      写入成功/业务失败
"""
from __future__ import annotations

import logging
from typing import Optional

from app.schemas.business import ModifyResult
from app.services.order.factory import run_sync
from app.services.order_modify.factory import get_order_modify_service_factory
from app.services.order_modify.protocols import MODIFIABLE_STATUSES, OrderModifyService

logger = logging.getLogger(__name__)

from app.services.agent_gating import get_approval_gate  # 写操作审批票据


# 状态中文名（提示语用；未知状态原样透出，避免误导）
_STATUS_LABEL = {
    "pending": "待支付",
    "paid": "已支付（未发货）",
    "shipped": "已发货",
    "delivered": "已签收",
    "completed": "已完成",
    "refunded": "已退款",
    "cancelled": "已取消",
}


# =============================================================
# 确认机制（统一 helper · spec §2.3）
# =============================================================
def _check_confirmation(confirmed: bool, action_desc: str) -> Optional[dict]:
    """写操作确认门禁（V2 简化版）。

    Args:
        confirmed: 用户是否已确认（True = 已确认；False = 未确认）
        action_desc: 操作中文描述，用于生成 prompt

    Returns:
        None: confirmed=True，可继续执行写操作
        dict: confirmed=False，返 needs_confirmation dict 让 LLM 询问用户

    设计：返回 dict 而非抛异常，因为 Tool 返 dict 是契约；dispatch 会把 dict 透传给 LLM。
    """
    if not confirmed:
        return {
            "status": "needs_confirmation",
            "prompt": f"即将{action_desc}，请用户回复「确认」后再执行。",
            "requires_user_input": True,
        }
    return None


# =============================================================
# 状态门禁：未发货可自助改，发货后转人工
# =============================================================
def _status_gate(
    user_id: Optional[int], order_no: str, action_label: str,
    session_id: Optional[str] = None,
) -> Optional[dict]:
    """写操作前置状态门禁。

    Returns:
        None    → 允许继续（pending/paid）
        dict    → 不可改：{"error": ...}（订单不存在/越权）或
                  {"status": "needs_handoff", ...}（已发货等，已建工单）

    Why 需要 session_id：工单按会话去重（同会话未结单则合并刷新上下文），
    没有 session_id 每次拦截都会新开一张单，坐席队列会被刷爆。
    """
    if not user_id:
        return {"error": "user_id is required"}
    try:
        from app.services.order.factory import get_order_service_factory

        svc = get_order_service_factory().get_order_service()
        order = run_sync(svc.get_order(user_id, order_no))
    except Exception as e:
        logger.warning(f"状态门禁查询订单失败: {type(e).__name__}: {e}")
        return {"error": f"订单状态查询失败，请稍后重试或转人工：{type(e).__name__}"}

    if order is None:
        # 不存在与越权合并成同一句，避免探测他人订单号
        return {"error": f"订单不存在或无权限访问：{order_no}"}

    if order.status in MODIFIABLE_STATUSES:
        return None

    label = _STATUS_LABEL.get(order.status, order.status)
    ticket_no = None
    try:
        from app.services.handoff_ticket_service import create_modify_handoff

        result = create_modify_handoff(
            user_id=user_id,
            session_id=session_id or "",
            order_no=order_no,
            order_status=order.status,
            action=action_label,
        )
        ticket_no = result.get("ticket_no")
    except Exception as e:  # 建单失败也要给用户明确答复，不能沉默
        logger.warning(f"转人工建单失败（继续返回拦截结果）: {type(e).__name__}: {e}")

    ticket_part = f"（工单号 {ticket_no}）" if ticket_no else ""
    return {
        "status": "needs_handoff",
        "handoff": True,
        "requires_user_input": False,
        "order_no": order_no,
        "order_status": order.status,
        "order_status_label": label,
        "ticket_no": ticket_no,
        "reason": f"订单状态为「{label}」，{action_label}不可自助修改，已转人工",
        "user_message": (
            f"这笔订单当前状态是「{label}」，{action_label}涉及库房与派件，"
            f"需要人工客服核实处理。已为您转接坐席{ticket_part}，客服会尽快跟进。"
        ),
    }


class OrderModifyTool:
    """售中订单修改 Tool（写操作 · 状态门禁 + 2 步确认）

    所有写方法：
    0. 状态门禁：非未发货 → 建工单返 needs_handoff（不写库）
    1. 默认 confirmed=False → 触发确认门禁，返 needs_confirmation dict
    2. 传入 confirmed=True → 校验审批票据 → 调 Service 执行实际写操作
    """

    # =============================================================
    # 1. 修改收货地址
    # =============================================================
    @staticmethod
    def modify_address(
        user_id: int, order_no: str, new_address: str, confirmed: bool = False,
        session_id: Optional[str] = None,
    ) -> dict:
        """修改收货地址（限未发货订单；已发货转人工）。

        Args:
            user_id: 用户 ID（防越权）
            order_no: 订单号
            new_address: 新地址字符串
            confirmed: 用户是否已确认（默认 False 触发确认提示）
            session_id: 会话 ID（转人工时用于工单去重，可空）

        Returns:
            needs_handoff dict / needs_confirmation dict / ModifyResult dict / {"error": ...}
        """
        if not new_address or not str(new_address).strip():
            return {"error": "新地址不能为空"}

        # Step 0: 状态门禁（必须在确认之前——避免"问了确认却改不了"）
        gate = _status_gate(user_id, order_no, "收货地址修改", session_id)
        if gate is not None:
            return gate

        # Step 1: 确认门禁
        action_desc = f"将订单 {order_no} 的收货地址改为「{new_address}」"
        confirmation = _check_confirmation(confirmed, action_desc)
        if confirmation is not None:
            confirmation["ticket"] = get_approval_gate().request(
                "order_modify.address", user_id,
                {"order_no": order_no, "new_address": new_address},
            )
            return confirmation

        # Step 1.5: 审批票据校验+消费（幂等：同票据不可二次执行）
        ok, reason = get_approval_gate().verify_and_consume(
            "order_modify.address", user_id,
            {"order_no": order_no, "new_address": new_address},
        )
        if not ok:
            return {"error": f"write_gated: {reason}",
                    "hint": "请先走确认流程重新发起（需重新生成审批票据）"}

        # Step 2: 调 Service 写
        try:
            svc: OrderModifyService = (
                get_order_modify_service_factory().get_order_modify_service()
            )
            result: ModifyResult = run_sync(
                svc.modify_address(user_id, order_no, new_address),
            )
            return result.model_dump()
        except Exception as e:
            logger.warning(
                f"OrderModifyTool.modify_address 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}

    # =============================================================
    # 2. 修改联系电话（写操作面收窄为"改地址/改电话"）
    # =============================================================
    @staticmethod
    def modify_contact(
        user_id: int, order_no: str, new_phone: str,
        new_name: Optional[str] = None, confirmed: bool = False,
        session_id: Optional[str] = None,
    ) -> dict:
        """修改收件人联系电话（限未发货订单；已发货转人工）。

        Args:
            user_id: 用户 ID（防越权）
            order_no: 订单号
            new_phone: 新手机号（11 位大陆号）
            new_name: 新收件人姓名（None = 不改）
            confirmed: 用户是否已确认
            session_id: 会话 ID（转人工工单去重）
        """
        if not new_phone or not str(new_phone).strip():
            return {"error": "新手机号不能为空"}

        gate = _status_gate(user_id, order_no, "联系电话修改", session_id)
        if gate is not None:
            return gate

        name_part = f"、收件人改为「{new_name}」" if new_name else ""
        action_desc = f"将订单 {order_no} 的联系电话改为「{new_phone}」{name_part}"
        confirmation = _check_confirmation(confirmed, action_desc)
        if confirmation is not None:
            confirmation["ticket"] = get_approval_gate().request(
                "order_modify.contact", user_id,
                {"order_no": order_no, "new_phone": new_phone, "new_name": new_name},
            )
            return confirmation

        ok, reason = get_approval_gate().verify_and_consume(
            "order_modify.contact", user_id,
            {"order_no": order_no, "new_phone": new_phone, "new_name": new_name},
        )
        if not ok:
            return {"error": f"write_gated: {reason}",
                    "hint": "请先走确认流程重新发起（需重新生成审批票据）"}

        try:
            svc: OrderModifyService = (
                get_order_modify_service_factory().get_order_modify_service()
            )
            result: ModifyResult = run_sync(
                svc.modify_contact(user_id, order_no, new_phone, new_name),
            )
            return result.model_dump()
        except Exception as e:
            logger.warning(
                f"OrderModifyTool.modify_contact 失败: {type(e).__name__}: {e}",
            )
            return {"error": f"tool execution failed: {type(e).__name__}: {str(e)[:200]}"}


# 暴露给测试用例（单测验证 confirmation dict 形态 / 门禁纯函数）
__all__ = ["OrderModifyTool", "_check_confirmation", "_status_gate"]
