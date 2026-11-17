"""
OrderModifyTool 测试（2026-11 改造后）

本域规则：
1. 写操作只有两个：改收货地址 / 改联系电话
2. 只有未发货（pending/paid）可自助改；已发货（含已发货未揽收）→ needs_handoff + 建工单

测试策略（为什么这么分层）：
- 确认流程与写调用：patch 掉状态门禁（`_status_gate`）→ 只测"确认 → 票据 → 写"这条链，
  否则每个用例都要造订单数据，失败原因不隔离
- 门禁本身：单独测 `_status_gate`，用假 OrderService 精确控制 status，验证
  「pending/paid 放行、shipped/delivered 拦截、越权报错、建单失败仍拦截」
- 两者分开测，任何一个红都指向明确的一层

env 兜底由 conftest.py 提供。
"""
from __future__ import annotations

from typing import List, Optional
from unittest.mock import patch

import pytest

from app.schemas.business import ModifyResult
from app.tools.order_modify_tool import OrderModifyTool, _check_confirmation, _status_gate


# =============================================================
# Mock Service（实现 OrderModifyService Protocol 的本域两个写方法）
# =============================================================
class MockOrderModifyService:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def modify_address(
        self, user_id: int, order_no: str, new_address: str,
    ) -> ModifyResult:
        self.calls.append(("modify_address", user_id, order_no, new_address))
        return ModifyResult(
            success=True, order_no=order_no, modification_type="address",
            reason="地址修改成功",
            before_snapshot={"shipping_address": "旧地址"},
            after_snapshot={"shipping_address": new_address},
        )

    async def modify_contact(
        self, user_id: int, order_no: str, new_phone: str,
        new_name: Optional[str] = None,
    ) -> ModifyResult:
        self.calls.append(("modify_contact", user_id, order_no, new_phone, new_name))
        return ModifyResult(
            success=True, order_no=order_no, modification_type="contact",
            reason=f"联系电话已修改为 {new_phone[:3]}****{new_phone[-4:]}",
            before_snapshot={"receiver_phone": None},
            after_snapshot={"receiver_phone": new_phone},
        )


@pytest.fixture
def mock_svc():
    """写链路测试：patch 服务工厂 + 放行状态门禁（门禁单独测）"""
    svc = MockOrderModifyService()
    factory_inst = type("F", (), {"get_order_modify_service": staticmethod(lambda: svc)})()
    with patch(
        "app.tools.order_modify_tool.get_order_modify_service_factory",
        return_value=factory_inst,
    ), patch(
        "app.tools.order_modify_tool._status_gate", return_value=None,
    ):
        yield svc


# =============================================================
# 改地址：确认 → 写
# =============================================================
def test_modify_address_unconfirmed_returns_needs_confirmation(mock_svc):
    """默认 confirmed=False → needs_confirmation，且不调 Service"""
    result = OrderModifyTool.modify_address(1001, "ORD001", "新地址 X")
    assert result["status"] == "needs_confirmation"
    assert result["requires_user_input"] is True
    assert "ORD001" in result["prompt"] and "新地址 X" in result["prompt"]
    assert len(mock_svc.calls) == 0


def test_modify_address_confirmed_executes_service(mock_svc):
    """confirmed=True → 调 Service → ModifyResult.model_dump()"""
    from app.services.agent_gating import get_approval_gate
    get_approval_gate().request(
        "order_modify.address", 1001, {"order_no": "ORD001", "new_address": "新地址 X"},
    )
    result = OrderModifyTool.modify_address(1001, "ORD001", "新地址 X", confirmed=True)
    assert result["success"] is True
    assert result["modification_type"] == "address"
    assert result["after_snapshot"]["shipping_address"] == "新地址 X"
    assert mock_svc.calls[-1] == ("modify_address", 1001, "ORD001", "新地址 X")


def test_modify_address_rejects_blank_address(mock_svc):
    """空白地址：本地直接拒，不进确认流程（省一轮对话）"""
    result = OrderModifyTool.modify_address(1001, "ORD001", "   ")
    assert result["error"] == "新地址不能为空"
    assert len(mock_svc.calls) == 0


# =============================================================
# 改电话：确认 → 写
# =============================================================
def test_modify_contact_unconfirmed_returns_needs_confirmation(mock_svc):
    result = OrderModifyTool.modify_contact(1001, "ORD001", "13900001234")
    assert result["status"] == "needs_confirmation"
    assert "13900001234" in result["prompt"]
    assert len(mock_svc.calls) == 0


def test_modify_contact_confirmed_executes_service(mock_svc):
    from app.services.agent_gating import get_approval_gate
    get_approval_gate().request(
        "order_modify.contact", 1001,
        {"order_no": "ORD001", "new_phone": "13900001234", "new_name": None},
    )
    result = OrderModifyTool.modify_contact(1001, "ORD001", "13900001234", confirmed=True)
    assert result["success"] is True
    assert result["modification_type"] == "contact"
    # 回复里手机号脱敏（服务层负责，mock 沿用同格式）
    assert "139****1234" in result["reason"]
    assert mock_svc.calls[-1] == ("modify_contact", 1001, "ORD001", "13900001234", None)


# =============================================================
# 状态门禁：未发货放行 / 已发货转人工
# =============================================================
class _FakeOrder:
    def __init__(self, status: str) -> None:
        self.status = status


class _FakeOrderService:
    def __init__(self, order) -> None:
        self._order = order
        self.calls = 0

    async def get_order(self, user_id: int, order_no: str):
        self.calls += 1
        return self._order


def _patch_order_service(order):
    svc = _FakeOrderService(order)
    factory = type("F", (), {"get_order_service": staticmethod(lambda: svc)})()
    return patch(
        "app.services.order.factory.get_order_service_factory", return_value=factory
    ), svc


@pytest.mark.parametrize("status", ["pending", "paid"])
def test_gate_allows_unshipped(status):
    """未发货（pending/paid）→ 放行（返 None）"""
    patcher, svc = _patch_order_service(_FakeOrder(status))
    with patcher:
        assert _status_gate(1001, "ORD001", "收货地址修改", "sess-1") is None
    assert svc.calls == 1


@pytest.mark.parametrize(
    "status,label",
    [
        ("shipped", "已发货"),          # 含「已发货未揽收」——业务明确要求转人工
        ("delivered", "已签收"),
        ("completed", "已完成"),
        ("refunded", "已退款"),
    ],
)
def test_gate_blocks_shipped_and_creates_handoff(status, label):
    """发货后 → needs_handoff + 建工单（工单号回给用户）"""
    patcher, _ = _patch_order_service(_FakeOrder(status))
    fake_handoff = {"ticket_id": 88, "ticket_no": "HFT-TEST0001", "created": True}
    with patcher, patch(
        "app.services.handoff_ticket_service.create_modify_handoff",
        return_value=fake_handoff,
    ) as m:
        gate = _status_gate(1001, "ORD001", "收货地址修改", "sess-abc")
    assert gate["status"] == "needs_handoff"
    assert gate["order_status"] == status
    assert gate["order_status_label"] == label
    assert gate["ticket_no"] == "HFT-TEST0001"
    assert gate["requires_user_input"] is False
    # 工单按会话去重：session_id 必须透传
    assert m.call_args.kwargs["session_id"] == "sess-abc"
    assert m.call_args.kwargs["order_no"] == "ORD001"
    # 用户可读话术里必须出现"人工"和工单号，且不能说"已修改"
    assert "人工" in gate["user_message"]
    assert "HFT-TEST0001" in gate["user_message"]
    assert "已帮您修改" not in gate["user_message"]


def test_gate_blocks_even_if_handoff_creation_fails():
    """建单失败也必须拦住（不能因为工单没建成而放行写操作）"""
    patcher, _ = _patch_order_service(_FakeOrder("shipped"))
    with patcher, patch(
        "app.services.handoff_ticket_service.create_modify_handoff",
        side_effect=RuntimeError("db down"),
    ):
        gate = _status_gate(1001, "ORD001", "联系电话修改", "sess-1")
    assert gate["status"] == "needs_handoff"
    assert gate["ticket_no"] is None
    # 没有工单号时话术仍要给出"转接坐席"，不能留空
    assert "坐席" in gate["user_message"]


def test_gate_rejects_unknown_order():
    """订单不存在/越权 → error（不建单；避免探测他人订单号）"""
    patcher, _ = _patch_order_service(None)
    with patcher, patch(
        "app.services.handoff_ticket_service.create_modify_handoff"
    ) as m:
        gate = _status_gate(1001, "ORD999", "收货地址修改", "sess-1")
    assert "订单不存在或无权限" in gate["error"]
    assert not m.called


def test_shipped_order_never_reaches_write_path():
    """端到端串一遍：已发货订单即使 confirmed=True 也绝不能调到 Service

    这里**不能**复用 mock_svc fixture（它把门禁 patch 成放行）——
    本用例要的正是门禁真实生效，所以自己起一套 patch。
    """
    class _Order:
        status = "shipped"

    async def _get_order(user_id, order_no):
        return _Order()

    order_svc = type("S", (), {"get_order": staticmethod(_get_order)})()
    order_fac = type("F", (), {"get_order_service": staticmethod(lambda: order_svc)})()

    write_svc = MockOrderModifyService()
    write_fac = type("W", (), {"get_order_modify_service": staticmethod(lambda: write_svc)})()

    with patch(
        "app.services.order.factory.get_order_service_factory", return_value=order_fac
    ), patch(
        "app.tools.order_modify_tool.get_order_modify_service_factory", return_value=write_fac
    ), patch(
        "app.services.handoff_ticket_service.create_modify_handoff",
        return_value={"ticket_id": 1, "ticket_no": "HFT-X", "created": True},
    ):
        result = OrderModifyTool.modify_address(1001, "ORD001", "新地址", confirmed=True)
    assert result["status"] == "needs_handoff"
    assert len(write_svc.calls) == 0  # 写方法一次都没被调用


# =============================================================
# helper / 分层约束
# =============================================================
def test_check_confirmation_helper():
    assert _check_confirmation(False, "测试动作") == {
        "status": "needs_confirmation",
        "prompt": "即将测试动作，请用户回复「确认」后再执行。",
        "requires_user_input": True,
    }
    assert _check_confirmation(True, "测试动作") is None


def test_order_modify_tool_no_models_import():
    """Tool 类禁止直接 import app.models.*（走 Service Protocol）"""
    import inspect
    from app.tools import order_modify_tool as mod

    source = inspect.getsource(mod)
    assert "from app.models" not in source
    assert "import app.models" not in source
