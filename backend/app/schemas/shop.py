"""
商品 / 订单公开 API 的 Pydantic schema

设计：
- cover_url 用相对路径 /products/{sku}.jpg（前端 public 目录）
- 订单详情聚合 items + logistics
"""
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# =============================================================
# 下单
# =============================================================
class CreateOrderRequest(BaseModel):
    """下单请求"""
    sku: str = Field(..., description="商品 SKU")
    qty: int = Field(1, ge=1, le=99, description="数量，默认 1")

    @field_validator("sku")
    @classmethod
    def _check_sku(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("sku 不能为空")
        return v


class OrderActionResponse(BaseModel):
    """订单状态流转响应（付款/发货/签收/退款）"""
    order_no: str
    status: str
    refund_no: Optional[str] = None  # 仅退款时有
    auto_delivery: Optional[dict] = None  # 虚拟商品支付事件的自动履约时间线


class RefundRequest(BaseModel):
    """退款申请请求"""
    reason: str = Field("用户申请退款", max_length=500, description="退款原因")
    remark: Optional[str] = Field(None, max_length=1000, description="备注说明")


# =============================================================
# 商品
# =============================================================
class ProductOut(BaseModel):
    """单个商品（公开）"""
    sku: str
    name: str
    price: float
    stock: int
    attributes: Optional[dict[str, Any]] = None
    description: Optional[str] = None
    cover_url: str = Field(..., description="机型封面（列表聚合用，同机型共用一张场景图）")
    # 2026-11 前端展示调整：每个 SKU 有独立主图 + 3 张细节图（带规格角标），
    # 规格切换时换图，避免同机型多个规格看起来完全一样。
    sku_cover_url: Optional[str] = Field(None, description="该 SKU 的独立主图；旧商品无此资源时为 None")
    sku_gallery: list[str] = Field(default_factory=list, description="该 SKU 的图集（主图 + 细节图）")
    # 2026-11 商品价格周期：price 已是当期生效价，下面字段给出依据与有效期
    price_valid_from: Optional[str] = Field(None, description="当期价目生效日")
    price_valid_to: Optional[str] = Field(None, description="当期价目失效日（NULL=长期）")
    price_version: Optional[str] = Field(None, description="价目版本号；None=回落基准价（未核实当期价）")
    price_label: Optional[str] = Field(None, description="可直接展示的价格口径说明")


class ProductListResponse(BaseModel):
    """商品列表响应"""
    products: list[ProductOut]
    total: int


# =============================================================
# 订单
# =============================================================
class OrderItemOut(BaseModel):
    """订单明细项"""
    sku: str
    product_name: str
    qty: int
    unit_price: float
    subtotal: float


class LogisticsOut(BaseModel):
    """物流信息"""
    order_no: str
    logistics_no: Optional[str] = None
    status: str
    last_location: Optional[str] = None
    trajectory: list[dict[str, Any]] = []


class OrderSummaryOut(BaseModel):
    """订单概要（列表用）"""
    order_no: str
    status: str
    total_amount: float
    create_time: Optional[str] = None
    item_count: int = Field(0, description="商品行数（item_count）")
    auto_delivery: Optional[dict] = None  # 虚拟商品已自动履约（含卡密脱敏）


class OrderListResponse(BaseModel):
    """订单列表响应"""
    orders: list[OrderSummaryOut]
    total: int


class OrderDetailOut(BaseModel):
    """订单详情（聚合主表 + items + 物流）"""
    order: OrderSummaryOut
    items: list[OrderItemOut] = []
    logistics: Optional[LogisticsOut] = None
    auto_delivery: Optional[dict] = None  # 虚拟商品已自动履约
