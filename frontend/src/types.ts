// =============================================================
// 后端 API 类型定义（与 backend/app/schemas/*.py 一一对应）
// =============================================================

/** 用户信息（来自 /auth/me，含 stats 字段） */
export interface User {
  id: number;
  username: string;
  display_name?: string | null;
  email?: string | null;
  role: string;
  status: number;
  create_time: string;
  // 可选：仅 /me 返回（UserOutStats），login 响应不带
  message_count?: number;
  conversation_count?: number;
}

/**
 * 长期记忆条目（来自 GET /api/me/profile）
 * 与后端 long_term_memory.list_facts() 一一对应。
 * active=false 表示"只在这个会话里出现过一次"，还没资格被记住——
 * 前端要显式标出来，否则用户会以为客服已经认定了这个偏好。
 */
export interface MemoryFact {
  key: string;
  /** 中文标签（尺码/身高/体重…），后端 KEY_LABELS 提供 */
  label: string;
  value: string;
  /** 改口前的旧值，用于展示"从 170 改成 175" */
  prev_value?: string | null;
  source_session_id?: string | null;
  hits: number;
  active: boolean;
  expire_at: string;
  create_time: string;
}

/** 会话列表项（来自 GET /conversations） */
export interface Conversation {
  session_id: string;
  /** 自动生成的标题（M9）：首条 user 消息前 20 字符 */
  title?: string | null;
  last_message: string | null;
  updated_at: string | null;
  message_count: number;
}

/** 单条消息（来自 GET /conversations/{sid}/messages） */
export interface Message {
  role: 'user' | 'assistant';
  content: string;
  contexts?: string[] | null;
  scores?: number[] | null;
  // 扩展：消息级 metadata（从 SSE meta 事件保存）
  intent?: string | null;
  entities?: Entities | null;
  tool_result_preview?: string | null;
  // SSE meta.card 字段（订单卡片推送）
  // 与后端 app/schemas/sse_card.py::OrderCardPayload 一一对应
  card?: OrderCardPayload | null;
  // SSE meta.handoff 字段（转人工兜底 payload）
  // 与后端 app/services/escalation_service.py::HandoffPayload 一一对应
  handoff?: HandoffPayload | null;
  // 商城人工直发：role=assistant 且 intent_snapshot.human=true → 人工客服消息
  human?: boolean;
  create_time: string;
}

/**
 * 转人工兜底 payload
 * 由后端 EscalationService.handoff() 生成，塞入 SSE meta.handoff 字段
 * 触发原因：
 * - user_requested：用户说"转人工"
 * - agent_unavailable：Agent 异常（V3+V2 都失败）
 * - business_rule：业务规则触发（质量问题无凭证等）
 */
export interface HandoffPayload {
  handoff_id: string;
  reason: 'user_requested' | 'agent_unavailable' | 'business_rule';
  reason_label: string;
  created_at: string;
  user_id: number;
  user_card: {
    user_id: number;
    total_orders: number;
    recent_order_count: number;
  };
  recent_orders: OrderSummary[];
  recent_messages: Array<{ role: string; content: string; ts?: string }>;
  current_intent: string | null;
  current_entities: Entities | null;
  agent_failure_context: {
    failed_stage: string;
    v3_error_class?: string;
    v3_error_msg?: string;
    v2_error_class?: string;
    v2_error_msg?: string;
    retry_count?: number;
  } | null;
  summary_text: string;
  // P0 高风险关键词命中后写入（向后兼容：未命中时为 null）
  priority?: 'P0' | 'P1' | 'P2' | null;
  category?: string | null;
  matched_keyword?: string | null;
  detected_category?: string | null;
}

/**
 * SSE 订单卡片 payload
 * 由后端 orchestrator._build_order_card_payload() 生成，塞入 SSE meta 事件
 * type=density 组合：
 * - order_list + list      → N 个候选订单（disambiguate picker）
 * - order_detail + mini    → 唯一订单详情（context_jump）
 */
export interface OrderCardPayload {
  type: 'order_list' | 'order_detail';
  density: 'mini' | 'list';
  reason: 'disambiguate' | 'proactive' | 'context_jump';
  items: OrderSummary[];
  truncated: boolean;
  /** mini 模式下记录已选订单号（向后兼容 OrderSummary） */
  resolved_order_no?: string | null;
}

/** Intent 分类器提取的实体（来自 /chat SSE meta） */
export interface Entities {
  order_no: string | null;
  sku: string | null;
  keywords: string[];
}

/** 分页消息响应 */
export interface MessagesPage {
  session_id: string;
  messages: Message[];
  has_more: boolean;
  next_cursor: number | null;
  limit: number;
}

// =============================================================
// 新增：商品 / 订单
// =============================================================
export interface Product {
  sku: string;
  name: string;
  price: number;
  stock: number;
  attributes?: Record<string, unknown> | null;
  description?: string | null;
  /** 款封面（同款多规格共用；列表按款聚合时用） */
  cover_url: string;
  /** 该 SKU 的独立主图（带规格角标）；详情页/单品卡优先用 */
  sku_cover_url?: string | null;
  /** 该 SKU 图集：主图 + 3 张细节图 */
  sku_gallery?: string[];
  /** 2026-11 价格周期：price 是当期生效价，以下字段给出依据 */
  price_valid_from?: string | null;
  price_valid_to?: string | null;
  price_version?: string | null;
  price_label?: string | null;
}

export interface ProductListResponse {
  products: Product[];
  total: number;
}

/** 商城列表按款聚合后的卡片数据（一个 model = N 个规格 SKU） */
export interface ModelGroup {
  model: string;
  category: string;
  name: string;
  description: string;
  cover: string;
  minPrice: number;
  maxPrice: number;
  specCount: number;
  defaultSku: string;
  warrantyBody?: number;
  warrantyKeyPart?: number;
  priceVersion?: string | null;
  priceValidTo?: string | null;
  specs: string[];
}

export interface OrderItem {
  sku: string;
  product_name: string;
  qty: number;
  unit_price: number;
  subtotal: number;
}

export interface Logistics {
  order_no: string;
  logistics_no?: string | null;
  status: string;
  last_location?: string | null;
  trajectory: Array<Record<string, unknown>>;
}

/**
 * 虚拟商品自动履约记录（V21-P0.2）
 * 与后端 app/services/delivery_service.py::get_record 返回结构一一对应
 */
export interface AutoDeliveryStep {
  name: string;
  detail: string;
  ok: boolean;
}

export interface AutoDelivery {
  order_no?: string;
  sku?: string | null;
  card_secret?: string | null;
  delivered_at?: string | null;
  status?: string;
  policy?: string;
  latency_ms?: number;
  steps?: AutoDeliveryStep[];
  duplicate?: boolean;
}

export interface OrderSummary {
  order_no: string;
  status: string;
  total_amount: number;
  create_time?: string | null;
  item_count: number;
  auto_delivery?: AutoDelivery | null;
}

export interface OrderListResponse {
  orders: OrderSummary[];
  total: number;
}

export interface OrderDetail {
  order: OrderSummary;
  items: OrderItem[];
  logistics?: Logistics | null;
  auto_delivery?: AutoDelivery | null;
}

/**
 * 订单状态流转响应（M10 闭环：创建/付款/发货/签收/退款）
 * 与后端 app/schemas/shop.py::OrderActionResponse 一一对应
 */
export interface OrderActionResponse {
  order_no: string;
  status: string;
  refund_no?: string | null;
}

/** 下单请求体（前端 -> POST /orders） */
export interface CreateOrderPayload {
  sku: string;
  qty: number;
}

/** 退款申请请求体（前端 -> POST /orders/:no/refund） */
export interface RefundPayload {
  reason?: string;
  remark?: string;
}

/** 注册请求体（前端 -> POST /auth/register） */
export interface RegisterPayload {
  username: string;
  password: string;
  display_name?: string;
  email?: string;
}

// =============================================================
// admin 运营聚合面板
// =============================================================
export interface DailyActivityPoint {
  date: string;
  conversations: number;
  active_users: number;
  messages: number;
}

export interface AdminLatencySummary {
  samples: number;
  p50_ms: number;
  p95_ms: number;
}

export interface AdminHandoffSummary {
  total: number;
  by_priority: Record<string, number>;
  by_category: Record<string, number>;
  coverage_complete: boolean;
  data_source: string;
}

export interface AdminHitAtKSummary {
  window_size: number;
  total_samples: number;
  hit_at_1: number;
  hit_at_3: number;
  hit_at_5: number;
  hit_at_10: number;
}

export interface AdminAnalyticsResponse {
  start_date: string;
  end_date: string;
  generated_at: string;
  cache_hit: boolean;
  cache_ttl_seconds: number;
  daily_activity: DailyActivityPoint[];
  latency: AdminLatencySummary;
  handoffs: AdminHandoffSummary;
  hit_at_k: AdminHitAtKSummary;
  limitations: string[];
}

// =============================================================
/** 换款候选卡片（只来自真实在售商品切片） */
export interface ProductChoice {
  model: string;
  sku: string;
  name: string;
  category: string;
  cover: string;
}

/** /chat SSE 流式事件（自定义 JSON 协议） */
export type StreamEvent =
  | {
      type: 'meta';
 // SSE Resume：每个 SSE event 的 seq（id: 行解析）
      id?: number;
 // SSE Resume：本回合 stream_id（前端 catch 时存到 sessionStorage 用于 resume）
      stream_id?: string;
      intent: string;
      entities: Entities;
      contexts: string[];
      scores: number[];
      // 扩展字段（按 intent 决定是否出现）
      products_found?: number;
      kb_hits?: number;
      policy_hits?: number;
      refundable?: boolean;
      reason?: string;
      order_no?: string;
      days_since_order?: number;
      v3_engine?: string;
      tool_result_preview?: string;
      // SSE 订单卡片（M14 OrderContextResolver 决策产物）
      card?: OrderCardPayload;
      // SSE 转人工 payload（Agent 异常 / 用户要求转人工）
      handoff?: HandoffPayload;
    }
  | { type: 'token'; id?: number; text: string }
  | { type: 'done'; id?: number; session_id: string; assistant_message_id?: number }
  | { type: 'error'; id?: number; message: string }
  | { type: 'heartbeat'; id?: number; ts: number }
  | { type: 'closed'; id?: number }
 // SSE Resume：resume 端点一次性重发已流 prefix
  | { type: 'resume_prefix'; id?: number; prefix_text: string; from_event_id: number; stream_id?: string }
  | { type: 'product_choices'; id?: number; choices: ProductChoice[]; current_model?: string | null };
