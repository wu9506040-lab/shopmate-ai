"""
边界回归 · with_safe_session 吞异常语义下的业务校验

背景（2026-11-15 审计结论）：
with_safe_session 会「吞块内异常 + rollback」。此前大量业务 raise 写在块内：
    - 单测用「不吞异常」的 fake session 替身 → 异常直接穿透 → 测试绿；
    - 生产真实现吞掉异常 → 代码继续往下走 → 超卖 / 重复退款 / 假成功 / None → 500。
测试替身与生产语义不一致，掩盖了全部问题。

本文件用「与生产同语义」的 fake（吞异常 + rollback）做回归：
任何把业务 raise 移回块内的改动都会在这里挂掉。

涉及修复：
- order_lifecycle.create_order   库存不足照样建单（超卖）
- order_lifecycle.refund         已退款校验失效（重复退款）
- order_modify._check_user_order 返回 (None, []) → 调用方 AttributeError
- order_modify.modify_*          写库行缺失 → 假成功 / 超长输入假成功
"""
import asyncio
import datetime
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.order import Order as OrderORM, OrderItem as OrderItemORM
from app.models.product import Product as ProductORM
from app.models.refund import Refund as RefundORM, RefundStatus
from app.services.order_lifecycle import OrderLifecycle, OrderLifecycleError
import app.services.order_lifecycle as lifecycle_module
from app.services.order_modify import mysql_impl as impl_module
from app.services.order_modify.mysql_impl import MySQLOrderModifyService
from app.schemas.business import ModifyNotAllowedError, OrderNotFoundError


# =============================================================
# 与生产同语义的 fake：块内异常 → rollback + 吞咽（真实现的行为）
# =============================================================
def make_swallowing(session):
    """复刻 with_safe_session 的真实语义：异常不穿透，仅回滚。

    旧测试替身「不吞异常」→ 块内 raise 直接穿透 → 测试绿但生产是 bug；
    本替身与生产对齐，把业务 raise 移回块内的改动会在这里立刻挂掉。
    """
    @contextmanager
    def _fake(commit=True):
        try:
            yield session
            if commit:
                session.commit()
        except Exception:
            session.rollback()
        finally:
            pass
    return _fake


@pytest.fixture
def swallow_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    now = datetime.datetime(2026, 11, 15, 10, 0, 0)

    s.add(ProductORM(id=1, sku="FS368998-M", name="猫与小鼠夹克 HM261139", price=2299.0,
                     stock=5, status=1, deleted=0))
    s.add(ProductORM(id=2, sku="HKZ26946-M", name="猫与小鼠弯刀裤 HKZ26946", price=3299.0,
                     stock=3, status=1, deleted=0))

    s.add(OrderORM(id=1, order_no="ORD_PAID", user_id=1001, status="paid",
                   total_amount=2299, shipping_address="旧地址",
                   create_time=now, update_time=now))
    s.add(OrderORM(id=2, order_no="ORD_REFUNDED", user_id=1001, status="refunded",
                   total_amount=3299, shipping_address="旧地址",
                   create_time=now, update_time=now))
    s.add(OrderORM(id=3, order_no="ORD_SHIPPED", user_id=1001, status="shipped",
                   total_amount=100, shipping_address="旧地址",
                   create_time=now, update_time=now))
    s.add(RefundORM(refund_no="RF0001", order_id=2, user_id=1001,
                    reason="首退", status=RefundStatus.COMPLETED.value,
                    amount=3299, create_time=now, update_time=now))
    s.commit()

    fake = make_swallowing(s)
    with patch.object(lifecycle_module, "with_safe_session", fake), \
         patch.object(impl_module, "with_safe_session", fake):
        yield s
    s.close()
    engine.dispose()


def _run(coro):
    return asyncio.run(coro)


# =============================================================
# create_order：库存不足必须拒绝（防超卖），商品不存在必须 404
# =============================================================
def test_create_order_oversell_rejected(swallow_session):
    """库存 5，买 99 → 抛「库存不足」，库存不变化、不产生订单（旧代码会超卖）"""
    with pytest.raises(OrderLifecycleError) as exc:
        OrderLifecycle.create_order(1001, "FS368998-M", 99)
    assert "库存不足" in str(exc.value)

    p = swallow_session.query(ProductORM).filter(ProductORM.sku == "FS368998-M").first()
    assert p.stock == 5  # 库存未被扣成负数
    orders = swallow_session.query(OrderORM).filter(
        OrderORM.order_no.like("ORD2026%")).all()
    assert orders == []  # 没有偷偷建单


def test_create_order_missing_sku_rejected(swallow_session):
    """SKU 不存在 → OrderLifecycleError 404（旧代码：吞掉后返回 None → API 500）"""
    with pytest.raises(OrderLifecycleError) as exc:
        OrderLifecycle.create_order(1001, "NO-SUCH-SKU", 1)
    assert exc.value.status_code == 404
    assert "不存在或已下架" in str(exc.value)


def test_create_order_success_still_works(swallow_session):
    """正常下单不受影响：库存扣减 + 订单创建"""
    result = OrderLifecycle.create_order(1001, "FS368998-M", 2)
    assert result["order_no"].startswith("ORD")
    assert result["status"] == "pending"
    p = swallow_session.query(ProductORM).filter(ProductORM.sku == "FS368998-M").first()
    assert p.stock == 3


# =============================================================
# refund：重复退款必须拒绝，订单不存在必须 404
# =============================================================
def test_refund_already_refunded_rejected(swallow_session):
    """已退款订单再退 → 409，且不产生第二条 refund 记录（旧代码会重复退款）"""
    with pytest.raises(OrderLifecycleError) as exc:
        OrderLifecycle.refund_order(1001, "ORD_REFUNDED", "再退一次")
    assert exc.value.status_code == 409
    assert "已退款" in str(exc.value)

    count = swallow_session.query(RefundORM).filter(
        RefundORM.order_id == 2).count()
    assert count == 1  # 只有种子里的 RF0001


def test_refund_missing_order_rejected(swallow_session):
    """订单不存在 → 404（旧代码：吞掉 → None → 500）"""
    with pytest.raises(OrderLifecycleError) as exc:
        OrderLifecycle.refund_order(1001, "ORD_NOPE", "退款")
    assert exc.value.status_code == 404


def test_refund_paid_order_success(swallow_session):
    """正常退款不受影响：paid → refunded + refund 记录"""
    result = OrderLifecycle.refund_order(1001, "ORD_PAID", "七天无理由")
    assert result["status"] == "refunded"
    o = swallow_session.query(OrderORM).filter(OrderORM.order_no == "ORD_PAID").first()
    assert o.status == "refunded"


# =============================================================
# order_modify：归属/状态校验必须抛业务异常，而不是返回 (None, [])
# =============================================================
def test_modify_missing_order_raises(swallow_session):
    """订单不存在 → OrderNotFoundError（旧代码：(None,[]) → AttributeError → 500）"""
    svc = MySQLOrderModifyService()
    with pytest.raises(OrderNotFoundError):
        _run(svc.modify_address(1001, "ORD_NOPE", "新地址"))


def test_modify_shipped_order_raises(swallow_session):
    """shipped 状态 → ModifyNotAllowedError（吞异常语义下也能正确抛出）"""
    svc = MySQLOrderModifyService()
    with pytest.raises(ModifyNotAllowedError):
        _run(svc.modify_address(1001, "ORD_SHIPPED", "新地址"))


def test_modify_address_too_long_rejected(swallow_session):
    """地址 > 500 字符（列宽 String(500)）→ 显式失败，不写库、不假成功"""
    svc = MySQLOrderModifyService()
    result = _run(svc.modify_address(1001, "ORD_PAID", "超" * 501))
    assert result.success is False
    assert "500" in result.reason
    o = swallow_session.query(OrderORM).filter(OrderORM.order_no == "ORD_PAID").first()
    assert o.shipping_address == "旧地址"


def test_modify_address_boundary_500_ok(swallow_session):
    """地址正好 500 字符 → 允许（边界值不误伤）"""
    svc = MySQLOrderModifyService()
    result = _run(svc.modify_address(1001, "ORD_PAID", "址" * 500))
    assert result.success is True
