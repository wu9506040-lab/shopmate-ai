"""
MySQL 默认实现 — MySQLOrderModifyService（-C）

实现 services/order_modify/protocols.py 的 OrderModifyService Protocol。
- 越权防护：所有写方法先调 OrderService.get_order(user_id, order_no) 校验归属
  （get_order 内部已做 user_id 过滤 + 软删过滤；不存在返 None → 抛 OrderNotFoundError）
- 状态校验：仅 MODIFIABLE_STATUSES（pending/paid）允许写
- 修改限制：分两种类型
  * modify_address  → UPDATE orders.shipping_address
  * modify_item_spec → UPDATE order_items.qty + subtotal（重算订单 total）
  * merge_orders     → 把被合并订单的 order_items 移入主订单 + 软删被合并订单

依赖（CLAUDE.md §7.2 分层）：
- 上游：OrderService Protocol（复用 S15 · 不直接 import mysql_impl）
- 客户端：mysql_client.with_safe_session（直接连 db 是允许的：infrastructure 层）
- 模型：Order + OrderItem ORM（models/order.py）
"""
import datetime
import logging
import re
from typing import List, Optional

from app.clients.mysql_client import with_safe_session
from app.models.order import Order as OrderORM, OrderItem as OrderItemORM
from app.services.order_modify.protocols import MODIFIABLE_STATUSES
from app.schemas.business import (
    MergeResult,
    ModifyNotAllowedError,
    ModifyResult,
    OrderNotFoundError,
    MergeConditionError,
)

logger = logging.getLogger(__name__)


def _now() -> datetime.datetime:
    """统一取「现在」（测试可 patch 替换为固定时间戳）

    注意：不要改为类属性或全局，保留 free function 便于 monkeypatch。
    """
    return datetime.datetime.now()


class _RollbackSignal(Exception):
    """合并订单写块的回滚信号。

    with_safe_session 吞块内异常并 rollback —— 我们利用这一点实现
    「多笔写要么全成功要么全回滚」：块内发现问题就抛 _RollbackSignal
    （触发 rollback 且被吞），块外再抛真实业务异常给调用方。
    """


# =============================================================
# 状态校验辅助
# =============================================================
# 允许自助修改的状态由 protocols.MODIFIABLE_STATUSES 定义（单一真相源）：
# 未发货（pending/paid）可改；已发货（含已发货未揽收）及之后一律转人工。

# 手机号：11 位大陆号（1 开头，第二位 3-9）。保留区号/分机不在本期范围。
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def mask_phone(phone: Optional[str]) -> str:
    """手机号脱敏：138****8000（日志/回复里不回显完整号码）"""
    if not phone:
        return "（未填写）"
    p = phone.strip()
    return f"{p[:3]}****{p[-4:]}" if len(p) >= 7 else "***"


def _check_user_order(user_id: int, order_no: str):
    """越权 + 状态校验（OrderService 的强校验纯函数版本）

    返回 (OrderORM, OrderItemORM-列表) 元组。
    - 订单不存在/越权 → 抛 OrderNotFoundError
    - 状态不允许修改 → 抛 ModifyNotAllowedError

    边界修复：raise 移到块外。原来 raise 写在 with_safe_session 块内会被吞，
    然后 `o.status`/`o.id` 继续崩（也被吞）→ 隐式返回 (None, []) →
    调用方 `o.shipping_address` 直接 AttributeError → 500。
    """
    result = None
    error: Exception | None = None
    with with_safe_session(commit=False) as db:
        o = db.query(OrderORM).filter(
            OrderORM.order_no == order_no,
            OrderORM.user_id == user_id,      # 越权防护
            OrderORM.deleted == 0,
        ).first()
        if o is None:
            error = OrderNotFoundError(
                f"订单不存在或无权限访问：order_no={order_no}"
            )
        else:
            items = db.query(OrderItemORM).filter(
                OrderItemORM.order_id == o.id,
                OrderItemORM.deleted == 0,
            ).all()
            # 状态校验（口径见 MODIFIABLE_STATUSES 注释：已发货含未揽收一律不可改）
            if o.status not in MODIFIABLE_STATUSES:
                status_label = {
                    "shipped": "已发货",
                    "delivered": "已签收",
                    "completed": "已完成",
                    "refunded": "已退款",
                }.get(o.status, o.status)
                error = ModifyNotAllowedError(
                    f"订单状态为「{status_label}」，不可修改"
                )
            else:
                result = (o, list(items))
    if error is not None:
        raise error
    return result


# =============================================================
# OrderModifyService 默认实现
# =============================================================
class MySQLOrderModifyService:
    """OrderModifyService Protocol 的 MySQL 默认实现"""

    async def modify_address(
        self, user_id: int, order_no: str, new_address: str,
    ) -> ModifyResult:
        o, _ = _check_user_order(user_id, order_no)
        clean = (new_address or "").strip()
        if not clean:
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="address",
                reason="新地址不能为空",
            )
        # 边界：orders.shipping_address 是 String(500)。超长会触发
        # DataError → 被 with_safe_session 吞掉 → 返回 success=True 的假成功
        # （实际没写进去）。必须在写库前显式拒绝。
        if len(clean) > 500:
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="address",
                reason=f"地址过长（{len(clean)} 字符，上限 500），请精简后重试",
            )
        old_addr = o.shipping_address
        not_found: Exception | None = None
        with with_safe_session(commit=True) as db:
            o2 = db.query(OrderORM).filter(OrderORM.id == o.id).first()
            if o2 is None:
                # 并发删除了（defensive）——块外抛，吞掉会变成假成功
                not_found = OrderNotFoundError(f"订单不存在：id={o.id}")
            else:
                o2.shipping_address = clean
        if not_found is not None:
            raise not_found
        return ModifyResult(
            success=True, order_no=order_no,
            modification_type="address",
            reason=f"地址已修改为「{clean}」",
            before_snapshot={"shipping_address": old_addr},
            after_snapshot={"shipping_address": clean},
        )

    async def modify_contact(
        self, user_id: int, order_no: str, new_phone: str,
        new_name: Optional[str] = None,
    ) -> ModifyResult:
        """修改收件人联系电话（限未发货订单）。

        为什么先校验格式再写：电话号码是派件唯一联系方式，写错等于让快递
        找不到人，比"改不了"更糟。格式不合法直接拒，不做"尽力而为"。
        """
        o, _ = _check_user_order(user_id, order_no)
        phone = (new_phone or "").strip().replace("-", "").replace(" ", "")
        if not _PHONE_RE.match(phone):
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="contact",
                reason="手机号格式不正确，请提供 11 位大陆手机号（如 13800001234）",
            )
        # 边界：receiver_name 列宽 String(64)，超长会被吞成假成功
        old_phone, old_name = o.receiver_phone, o.receiver_name
        new_name_clean = (new_name or "").strip() or old_name
        if new_name_clean and len(new_name_clean) > 64:
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="contact",
                reason=f"收件人姓名过长（{len(new_name_clean)} 字符，上限 64）",
            )
        not_found: Exception | None = None
        with with_safe_session(commit=True) as db:
            o2 = db.query(OrderORM).filter(OrderORM.id == o.id).first()
            if o2 is None:
                not_found = OrderNotFoundError(f"订单不存在：id={o.id}")
            else:
                o2.receiver_phone = phone
                o2.receiver_name = new_name_clean
        if not_found is not None:
            raise not_found
        name_part = f"收件人「{new_name_clean}」" if new_name_clean else "收件人"
        return ModifyResult(
            success=True, order_no=order_no,
            modification_type="contact",
            reason=f"{name_part}联系电话已修改为 {mask_phone(phone)}",
            before_snapshot={"receiver_phone": old_phone, "receiver_name": old_name},
            after_snapshot={"receiver_phone": phone, "receiver_name": new_name_clean},
        )

    async def modify_item_spec(
        self, user_id: int, order_no: str, sku: str, new_qty: Optional[int] = None,
    ) -> ModifyResult:
        o, items = _check_user_order(user_id, order_no)
        target = next((i for i in items if i.sku == sku and i.deleted == 0), None)
        if target is None:
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="spec",
                reason=f"SKU 「{sku}」不在该订单中",
            )
        # new_qty=None 表示不改数量
        if new_qty is None:
            return ModifyResult(
                success=True, order_no=order_no,
                modification_type="spec",
                reason="未指定新数量，无修改",
                before_snapshot={"sku": sku, "qty": target.qty},
                after_snapshot={"sku": sku, "qty": target.qty},
            )
        if new_qty <= 0:
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="spec",
                reason="新数量必须大于 0",
            )
        # 边界：数量上限 999（与商城下单 le=99 口径一致偏宽），防异常值
        # 把 total_amount 推到天文数字；加量不校库存是已知业务留白（记录在案）
        if new_qty > 999:
            return ModifyResult(
                success=False, order_no=order_no,
                modification_type="spec",
                reason="新数量过大（上限 999），如确有需求请转人工处理",
            )
        old_qty = target.qty
        old_subtotal = float(target.subtotal)
        unit_price = float(target.unit_price)
        new_subtotal = round(unit_price * new_qty, 2)
        delta = new_subtotal - old_subtotal
        not_found: Exception | None = None
        with with_safe_session(commit=True) as db:
            item = db.query(OrderItemORM).filter(OrderItemORM.id == target.id).first()
            order_row = db.query(OrderORM).filter(OrderORM.id == o.id).first()
            if item is None or order_row is None:
                # 并发删除：块外抛，避免吞掉后返回假成功
                not_found = OrderNotFoundError(
                    f"订单数据已被并发修改：order_no={order_no}"
                )
            else:
                item.qty = new_qty
                item.subtotal = new_subtotal
                # 同步订单总金额
                order_row.total_amount = round(float(order_row.total_amount) + delta, 2)
        if not_found is not None:
            raise not_found
        return ModifyResult(
            success=True, order_no=order_no,
            modification_type="spec",
            reason=f"SKU 「{sku}」数量从 {old_qty} 调整为 {new_qty}",
            before_snapshot={"sku": sku, "qty": old_qty, "subtotal": old_subtotal},
            after_snapshot={"sku": sku, "qty": new_qty, "subtotal": new_subtotal},
        )

    async def merge_orders(
        self, user_id: int, order_nos: List[str],
    ) -> MergeResult:
        # 参数校验
        if not order_nos or len(order_nos) < 2:
            raise MergeConditionError("合并订单至少需要 2 个订单号")

        # 预校验所有订单存在 + 越权 + 未发货
        # 边界修复：错误收集 + 块外抛。原来块内 raise 被吞后循环继续，
        # rows 缺员 → rows[1:] 切片错位 → 用「不完整的订单集合」执行合并还报成功。
        precheck_error: Exception | None = None
        rows: List[OrderORM] = []
        with with_safe_session(commit=False) as db:
            for ono in order_nos:
                o = db.query(OrderORM).filter(
                    OrderORM.order_no == ono,
                    OrderORM.user_id == user_id,
                    OrderORM.deleted == 0,
                ).first()
                if o is None:
                    precheck_error = OrderNotFoundError(
                        f"订单不存在或无权限访问：order_no={ono}"
                    )
                    break
                if o.status not in MODIFIABLE_STATUSES:
                    precheck_error = MergeConditionError(
                        f"订单 {ono} 状态为 {o.status}，不可合并"
                    )
                    break
                rows.append(o)

            if precheck_error is None:
                # 条件 1：5 分钟时间窗（以最早订单 create_time 为锚）
                rows.sort(key=lambda r: r.create_time)
                earliest = rows[0]
                now = _now()
                window = datetime.timedelta(minutes=5)
                if earliest.create_time is None:
                    precheck_error = MergeConditionError(
                        f"订单 {earliest.order_no} 缺少创建时间，不可合并"
                    )
                elif now - earliest.create_time > window:
                    precheck_error = MergeConditionError(
                        f"订单最早创建于 {earliest.create_time.isoformat()}，"
                        f"超过 {int(window.total_seconds() // 60)} 分钟，不可合并"
                    )

            if precheck_error is None:
                # 条件 2：同店（用 product_id 集合交集作为「同一店铺」近似；
                # 接入店铺表后用真实 store_id）
                primary = rows[0]
                items_by_order: dict[int, List[OrderItemORM]] = {}
                for r in rows:
                    items = db.query(OrderItemORM).filter(
                        OrderItemORM.order_id == r.id,
                        OrderItemORM.deleted == 0,
                    ).all()
                    items_by_order[r.id] = list(items)

                primary_stores = {i.product_id for i in items_by_order[primary.id]}
                for r in rows[1:]:
                    other_stores = {i.product_id for i in items_by_order[r.id]}
                    if primary_stores & other_stores == set() and (primary_stores and other_stores):
                        # 至少两边都有商品，且交集为空 = 跨店（简化判定）
                        precheck_error = MergeConditionError(
                            f"订单 {primary.order_no} 与 {r.order_no} 不在同一店铺"
                        )
                        break
                    # 合并店铺集合
                    primary_stores |= other_stores

        if precheck_error is not None:
            raise precheck_error

        # 通过所有条件 → 执行合并（多笔写，出错整块回滚）
        merged_ids: List[str] = [r.order_no for r in rows[1:]]
        write_error: Exception | None = None
        with with_safe_session(commit=True) as db:
            for r in rows[1:]:
                # 把被合并订单的 items 移到主订单
                items = db.query(OrderItemORM).filter(
                    OrderItemORM.order_id == r.id,
                    OrderItemORM.deleted == 0,
                ).all()
                for it in items:
                    it.order_id = primary.id
                # 软删被合并订单
                row = db.query(OrderORM).filter(OrderORM.id == r.id).first()
                if row is None:
                    # 并发删除：抛信号触发 rollback（会被 with_safe_session 吞掉），
                    # 块外再抛真实异常 —— 避免吞掉后带着半套数据继续并报成功
                    write_error = OrderNotFoundError(f"订单不存在：id={r.id}")
                    raise _RollbackSignal()
                row.deleted = 1
                # 标记状态（业务可见：避免再次误合）
                # 简化：保留 status=pending/paid；deleted=1 即视为无效
            # 重算主订单总金额
            items = db.query(OrderItemORM).filter(
                OrderItemORM.order_id == primary.id,
                OrderItemORM.deleted == 0,
            ).all()
            new_total = round(sum(float(i.subtotal) for i in items), 2)
            main = db.query(OrderORM).filter(OrderORM.id == primary.id).first()
            if main is None:
                write_error = OrderNotFoundError(f"主订单不存在：id={primary.id}")
                raise _RollbackSignal()
            main.total_amount = new_total

        if write_error is not None:
            raise write_error

        return MergeResult(
            success=True,
            primary_order_no=primary.order_no,
            merged_order_nos=merged_ids,
            reason=f"已合并 {len(merged_ids)} 个订单到主订单 {primary.order_no}",
        )
