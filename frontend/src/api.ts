// =============================================================
// API 封装 - 与后端 FastAPI 通信
// 设计要点：
// 1. 所有 fetch 带 credentials:'include'（自动携带 httpOnly Cookie）
// 2. 流式用 fetch + ReadableStream，不用 EventSource（要支持 POST body）
// 3. 错误统一抛 Error，调用方 try/catch
// =============================================================
import type {
  AdminAnalyticsResponse,
  Conversation,
  CreateOrderPayload,
  MemoryFact,
  MessagesPage,
  OrderActionResponse,
  OrderDetail,
  OrderListResponse,
  Product,
  ProductListResponse,
  RefundPayload,
  RegisterPayload,
  StreamEvent,
  User,
} from './types';
import { ref, type Ref } from 'vue';

/** API 基础路径（企业级 /api/ 前缀分层，与 SPA 路由空间隔离） */
const API = '/api';

/**
 * 登录态缓存（解决 httpOnly Cookie 不可被 document.cookie 读取的问题）
 * 设计：API 层维护唯一可信的登录状态。
 * - 初始 null：未知（路由守卫首次调 /auth/me 探测）
 * - true：已登录（login/register 成功后置 true，getMe 返回有效用户）
 * - false：未登录（getMe 返回 401 或 logout 后）
 */
export const isAuthed: Ref<boolean | null> = ref(null);

// =============================================================
// 管理台 Bearer 身份通道（双端共存的关键设计）
// 商城身份走 httpOnly Cookie（全浏览器共享）；管理台身份走
// sessionStorage + Authorization Bearer（按 tab 隔离）。
// 两个通道互不覆盖 → 管理员后台与买家商城可同浏览器同时在线。
// =============================================================
const ADMIN_TOKEN_KEY = 'cs_admin_token';

export function getAdminToken(): string | null {
  try {
    return sessionStorage.getItem(ADMIN_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setAdminToken(token: string): void {
  try {
    sessionStorage.setItem(ADMIN_TOKEN_KEY, token);
  } catch {
    /* sessionStorage 不可用 → 静默失败（登录后仍能用 cookie 兜底） */
  }
}

export function clearAdminToken(): void {
  try {
    sessionStorage.removeItem(ADMIN_TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/**
 * 管理台请求注入 Bearer 头：
 * 仅对 /admin* 管理接口与 /auth/me（身份探测）生效，
 * 商城业务请求不受影响。
 */
function withAdminAuth(
  path: string,
  headers: HeadersInit,
): HeadersInit {
  const token = getAdminToken();
  if (token && (path.startsWith('/admin') || path.startsWith('/auth/me'))) {
    return {
      ...(headers as Record<string, string>),
      Authorization: `Bearer ${token}`,
    };
  }
  return headers;
}

/** 统一 fetch 包装：带 cookie + JSON */
export async function http<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    credentials: 'include',
    ...init,
    headers: withAdminAuth(path, {
      ...(init?.headers ?? {}),
    }),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      // FastAPI 422 的 detail 是数组 [{loc,msg,...}]，需转成可读文案
      const d = body.detail;
      if (typeof d === 'string') detail = d;
      else if (Array.isArray(d) && d.length > 0) {
        const first = d[0] ?? {};
        const field = Array.isArray(first.loc) ? String(first.loc[first.loc.length - 1]) : '';
        const msg = String(first.msg ?? '参数校验失败');
        detail = field ? `${field}: ${msg}` : msg;
        if (detail.includes('min_length') || detail.includes('at least')) detail = '密码至少 8 位';
        if (detail.includes('pattern') || detail.includes('username')) detail = '用户名只能用字母、数字、下划线（3-64 位）';
      } else if (d && typeof d === 'object') {
        detail = JSON.stringify(d);
      }
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

// =============================================================
// Auth
// =============================================================
export async function login(
  username: string,
  password: string,
): Promise<User> {
  const form = new URLSearchParams();
  form.append('username', username);
  form.append('password', password);
  const data = await http<{ user: User }>('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: form.toString(),
  });
  isAuthed.value = true;  // 同步登录态
  return data.user;
}

/** 注册（M9 新增） */
export async function register(payload: RegisterPayload): Promise<User> {
  const data = await http<{ user: User }>('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  isAuthed.value = true;  // 同步登录态（注册接口不返 cookie，由调用方再 login）
  return data.user;
}

/**
 * 一键 demo 体验（M13 cloud）
 * 后端自动创建 visitor_<uuid> 账号 + 立即发 token，
 * 前端无需注册，直接进系统。
 * 适用场景：公网公开 demo（云平台简历展示用）
 * 失败：服务端 ENABLE_DEMO_LOGIN=false 或网络错误
 */
export async function demoLogin(): Promise<User> {
  const data = await http<{ user: User }>('/public/demo-account', {
    method: 'POST',
  });
  isAuthed.value = true;
  return data.user;
}

export async function logout(): Promise<void> {
  await http<{ message: string }>('/auth/logout', { method: 'POST' });
  isAuthed.value = false;  // 同步登出态
}

/** 获取当前登录用户，未登录返回 null；同时更新 isAuthed 缓存 */
export async function getMe(): Promise<User | null> {
  // 管理台 tab 存在 Bearer token 时优先用它探测身份（cookie 不受影响）
  const token = getAdminToken();
  const headers: Record<string, string> = token
    ? { Authorization: `Bearer ${token}` }
    : {};
  const res = await fetch(`${API}/auth/me`, { credentials: 'include', headers });
  if (res.status === 401) {
    isAuthed.value = false;
    return null;
  }
  if (!res.ok) throw new Error(`getMe failed: ${res.status}`);
  const user = (await res.json()) as User;
  isAuthed.value = true;
  return user;
}

// =============================================================
// 我的长期记忆（L3）
// 查看权 + 删除权必须成对暴露：只给"记住"不给"删掉"的档案功能，
// 是客服系统最容易踩的信任坑。
// =============================================================
export async function getMyMemoryFacts(): Promise<{
  count: number;
  facts: MemoryFact[];
}> {
  return http('/me/profile');
}

/** key 不传 = 全量删除并写墓碑（之后不会再被自动写入） */
export async function deleteMyMemoryFact(key?: string): Promise<{
  deleted: number;
  key: string;
}> {
  const qs = key ? `?key=${encodeURIComponent(key)}` : '';
  return http(`/me/profile${qs}`, { method: 'DELETE' });
}

// =============================================================
// Conversations
// =============================================================
export async function listConversations(): Promise<{
  conversations: Conversation[];
  total: number;
}> {
  return http('/conversations');
}

export async function getMessages(
  sessionId: string,
  cursor?: number,
  limit = 20,
): Promise<MessagesPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (cursor !== undefined) params.append('cursor', String(cursor));
  return http(`/conversations/${encodeURIComponent(sessionId)}/messages?${params}`);
}

export async function deleteConversation(
  sessionId: string,
): Promise<{ session_id: string; message: string }> {
  return http(`/conversations/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });
}

/** 更新会话标题（M9 自动生成标题用） */
export async function updateConversationTitle(
  sessionId: string,
  title: string,
): Promise<{ session_id: string; title: string; message: string }> {
  return http(`/conversations/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
}

// =============================================================
// Shop（M9 新增：商品 / 订单公开 API）
// =============================================================
export async function listProducts(params?: {
  category?: string;
  limit?: number;
}): Promise<ProductListResponse> {
  const search = new URLSearchParams();
  if (params?.category) search.append('category', params.category);
  if (params?.limit) search.append('limit', String(params.limit));
  const qs = search.toString();
  return http(`/products${qs ? `?${qs}` : ''}`);
}

export async function getProduct(sku: string): Promise<Product> {
  return http(`/products/${encodeURIComponent(sku)}`);
}

export async function listMyOrders(params?: {
  status?: string;
  limit?: number;
}): Promise<OrderListResponse> {
  const search = new URLSearchParams();
  if (params?.status) search.append('status', params.status);
  if (params?.limit) search.append('limit', String(params.limit));
  const qs = search.toString();
  return http(`/orders/my${qs ? `?${qs}` : ''}`);
}

export async function getOrderDetail(orderNo: string): Promise<OrderDetail> {
  return http(`/orders/${encodeURIComponent(orderNo)}`);
}

// =============================================================
// 订单状态流转（M10 闭环 demo）
// 对应后端 /api/orders 状态机：pending → paid → shipped → delivered → refunded
// =============================================================
export async function createOrder(
  payload: CreateOrderPayload,
): Promise<OrderActionResponse> {
  return http('/orders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function payOrder(orderNo: string): Promise<OrderActionResponse> {
  return http(`/orders/${encodeURIComponent(orderNo)}/pay`, {
    method: 'POST',
  });
}

export async function shipOrder(orderNo: string): Promise<OrderActionResponse> {
  return http(`/orders/${encodeURIComponent(orderNo)}/ship`, {
    method: 'POST',
  });
}

export async function confirmOrder(orderNo: string): Promise<OrderActionResponse> {
  return http(`/orders/${encodeURIComponent(orderNo)}/confirm`, {
    method: 'POST',
  });
}

export async function refundOrder(
  orderNo: string,
  payload?: RefundPayload,
): Promise<OrderActionResponse> {
  return http(`/orders/${encodeURIComponent(orderNo)}/refund`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  });
}

// =============================================================
// Admin analytics（P4-2）
// =============================================================
export async function getAdminAnalytics(
  startDate: string,
  endDate: string,
): Promise<AdminAnalyticsResponse> {
  const params = new URLSearchParams({
    start_date: startDate,
    end_date: endDate,
  });
  return http(`/admin/analytics?${params}`);
}

// =============================================================
// Metrics（M9 demo 首页用：展示能力指标）
// =============================================================
export interface MetricsSnapshot {
  uptime_seconds: number;
  chat?: {
    total: number;
    by_intent: Record<string, number>;
    latency_ms?: { p50: number; p90: number; p95: number; max: number; samples: number };
  };
  rag?: { vector_search_success: number; vector_search_total: number };
  embedding?: { calls_total: number; errors_total: number };
  hit_at_k?: {
    window_size: number;
    total_samples: number;
    'hit@1': number;
    'hit@3': number;
    'hit@5': number;
    'hit@10': number;
  };
}

export async function getMetrics(): Promise<MetricsSnapshot> {
  return http('/metrics');
}

// =============================================================
// Chat - SSE 流式（核心）
// =============================================================
/**
 * 流式对话（支持 sku/order_no context 透传）
 * - 从 /shop/:sku 跳转带 ?sku=ZP1 → 后端注入【当前商品】到 prompt
 * - 从订单卡片跳转带 ?order_no=ORD... → 后端注入【当前订单】到 prompt
 * - 用户追问时会带当前 session_id（后端可继续注入同 context）
 * SSE Resume：
 * - 后端每条 SSE event 加 `id: {seq}\n` 行；前端解析后挂到 event.id
 * - 调用方在 catch（异常退出）时若未收到 done，可根据 event.id + 已收 token 文本
 *   调 resumeChat() 续传（用户视角完全无感）
 */
export async function* streamChat(
  query: string,
  sessionId?: string,
  opts?: { sku?: string; model?: string; orderNo?: string },
): AsyncGenerator<StreamEvent, void, void> {
  const res = await fetch(`${API}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    credentials: 'include',
    body: JSON.stringify({
      query,
      session_id: sessionId ?? null,
      // 用户从商品详情/订单卡片跳转过来时携带 context
      sku: opts?.sku ?? null,
      model: opts?.model ?? null,
      order_no: opts?.orderNo ?? null,
    }),
  });

  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      // FastAPI 422 的 detail 是数组 [{loc,msg,...}]，需转成可读文案
      const d = body.detail;
      if (typeof d === 'string') detail = d;
      else if (Array.isArray(d) && d.length > 0) {
        const first = d[0] ?? {};
        const field = Array.isArray(first.loc) ? String(first.loc[first.loc.length - 1]) : '';
        const msg = String(first.msg ?? '参数校验失败');
        detail = field ? `${field}: ${msg}` : msg;
        if (detail.includes('min_length') || detail.includes('at least')) detail = '密码至少 8 位';
        if (detail.includes('pattern') || detail.includes('username')) detail = '用户名只能用字母、数字、下划线（3-64 位）';
      } else if (d && typeof d === 'object') {
        detail = JSON.stringify(d);
      }
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE 协议：事件之间用 \n\n 分隔
      // 用 split('\n\n') 切，最后一段可能不完整，留到 buffer
      const parts = buffer.split('\n\n');
      buffer = parts.pop() ?? '';

      for (const part of parts) {
 // SSE Resume：解析 `id:` 行（每 event 可选）+ `data:` 行
        // 一个 event 可能由多行组成（id + data + event 等），按行分别处理
        let currentId: number | undefined;
        let currentData: string | null = null;
        for (const line of part.split('\n')) {
          const trimmed = line.trim();
          if (trimmed.startsWith('id:')) {
            const n = parseInt(trimmed.slice(3).trim(), 10);
            if (!Number.isNaN(n)) currentId = n;
          } else if (trimmed.startsWith('data:')) {
            currentData = trimmed.slice(5).trim();
          }
        }
        if (!currentData) continue;
        try {
          const event = JSON.parse(currentData) as StreamEvent;
          if (currentId !== undefined) {
            (event as StreamEvent & { id?: number }).id = currentId;
          }
          yield event;
        } catch (e) {
          console.warn('SSE 解析失败:', currentData, e);
        }
      }
    }

    // 收尾：处理 buffer 里残留的最后一段
    const tail = buffer.trim();
    if (tail.startsWith('data:') || tail.includes('\nid:') || tail.includes('\ndata:')) {
      let currentId: number | undefined;
      let currentData: string | null = null;
      for (const line of tail.split('\n')) {
        const trimmed = line.trim();
        if (trimmed.startsWith('id:')) {
          const n = parseInt(trimmed.slice(3).trim(), 10);
          if (!Number.isNaN(n)) currentId = n;
        } else if (trimmed.startsWith('data:')) {
          currentData = trimmed.slice(5).trim();
        }
      }
      if (currentData) {
        try {
          const event = JSON.parse(currentData) as StreamEvent;
          if (currentId !== undefined) {
            (event as StreamEvent & { id?: number }).id = currentId;
          }
          yield event;
        } catch {
          /* ignore */
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// =============================================================
 // SSE Resume（SSE Resume）
// =============================================================
/**
 * 流式中断续传
 * - 客户端在 streamChat catch 且未收到 done 时调用
 * - 后端从 Redis checkpoint 读取 prefix_text 一次性重发（resume_prefix 事件）
 * - 不调 LLM 续写（用户拍板 MVP 边界：checkpoint 重发即可）
 * - 同 (sessionId, streamId) 最多 resume 2 次（后端 410 限流）
 * 用法：
 *   try { yield* streamChat(...) }
 *   catch (e) {
 *     if (!hasReceivedDone && streamId && accText) {
 *       yield* resumeChat(sessionId, streamId, query, lastEventId, opts)
 *     } else { throw e }
 *   }
 */
export async function* resumeChat(
  sessionId: string,
  streamId: string,
  query: string,
  lastEventId: number | undefined,
  opts?: { sku?: string; model?: string; orderNo?: string },
): AsyncGenerator<StreamEvent, void, void> {
  const res = await fetch(`${API}/chat/resume`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    credentials: 'include',
    body: JSON.stringify({
      session_id: sessionId,
      stream_id: streamId,
      query,
      last_event_id: lastEventId ?? null,
      sku: opts?.sku ?? null,
      model: opts?.model ?? null,
      order_no: opts?.orderNo ?? null,
    }),
  });

  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      // FastAPI 422 的 detail 是数组 [{loc,msg,...}]，需转成可读文案
      const d = body.detail;
      if (typeof d === 'string') detail = d;
      else if (Array.isArray(d) && d.length > 0) {
        const first = d[0] ?? {};
        const field = Array.isArray(first.loc) ? String(first.loc[first.loc.length - 1]) : '';
        const msg = String(first.msg ?? '参数校验失败');
        detail = field ? `${field}: ${msg}` : msg;
        if (detail.includes('min_length') || detail.includes('at least')) detail = '密码至少 8 位';
        if (detail.includes('pattern') || detail.includes('username')) detail = '用户名只能用字母、数字、下划线（3-64 位）';
      } else if (d && typeof d === 'object') {
        detail = JSON.stringify(d);
      }
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      const parts = buffer.split('\n\n');
      buffer = parts.pop() ?? '';

      for (const part of parts) {
        let currentId: number | undefined;
        let currentData: string | null = null;
        for (const line of part.split('\n')) {
          const trimmed = line.trim();
          if (trimmed.startsWith('id:')) {
            const n = parseInt(trimmed.slice(3).trim(), 10);
            if (!Number.isNaN(n)) currentId = n;
          } else if (trimmed.startsWith('data:')) {
            currentData = trimmed.slice(5).trim();
          }
        }
        if (!currentData) continue;
        try {
          const event = JSON.parse(currentData) as StreamEvent;
          if (currentId !== undefined) {
            (event as StreamEvent & { id?: number }).id = currentId;
          }
          yield event;
        } catch (e) {
          console.warn('SSE resume 解析失败:', currentData, e);
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// =============================================================
// 人工介入坐席工作台（admin）
// =============================================================
export interface HandoffTicketBrief {
  id: number;
  ticket_no: string;
  session_id: string;
  user_id: number;
  reason: string;
  /** 2026-11：坐席可读的转人工原因（中文标签 + 触发依据） */
  reason_label?: string | null;
  reason_detail?: string | null;
  category?: string | null;
  priority: string;
  matched_keyword?: string | null;
  status: string;
  assignee?: string | null;
  auto_closed?: number | boolean;
  last_agent_reply_at?: string | null;
  create_time?: string | null;
  update_time?: string | null;
}

export interface HandoffTicketDetail extends HandoffTicketBrief {
  summary?: Record<string, unknown> | null;
  reply?: string | null;
  auto_close_seconds?: number;
  auto_close_at?: string | null;
  auto_close_remaining_sec?: number | null;
  messages: {
    id: number;
    role: string;
    content: string;
    create_time?: string | null;
    is_human_agent: boolean;
  }[];
}

export interface HandoffListResp {
  total: number;
  page: number;
  page_size: number;
  items: HandoffTicketBrief[];
}

/** 工单队列（status 可选 pending/taken/resolved/closed） */
export async function getHandoffTickets(
  status?: string,
  page = 1,
): Promise<HandoffListResp> {
  const params = new URLSearchParams({ page: String(page), page_size: '20' });
  if (status) params.set('status', status);
  return http(`/admin/handoff/tickets?${params}`);
}

/** 工单详情 + 会话上下文 */
export async function getHandoffTicket(id: number): Promise<HandoffTicketDetail> {
  return http(`/admin/handoff/tickets/${id}`);
}

/** 认领 */
export async function takeHandoffTicket(
  id: number,
): Promise<{ ok: boolean; ticket: HandoffTicketBrief }> {
  return http(`/admin/handoff/tickets/${id}/take`, { method: 'POST' });
}

/** 发送一条人工回复（工单保持「处理中」，可多轮沟通） */
export async function sendHandoffReply(
  id: number,
  reply: string,
): Promise<{ ok: boolean; ticket: HandoffTicketBrief }> {
  return http(`/admin/handoff/tickets/${id}/reply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reply }),
  });
}

/** 处理完成，结单（可带最后一条回复；不带回复则直接结束） */
export async function resolveHandoffTicket(
  id: number,
  reply?: string,
): Promise<{ ok: boolean; ticket: HandoffTicketBrief }> {
  return http(`/admin/handoff/tickets/${id}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reply: reply ?? '' }),
  });
}

/** 直接关闭（误触发/用户离开） */
export async function closeHandoffTicket(
  id: number,
): Promise<{ ok: boolean; ticket: HandoffTicketBrief }> {
  return http(`/admin/handoff/tickets/${id}/close`, { method: 'POST' });
}

// =============================================================
// 多渠道账号管理（管理台 · 按平台分组 + 凭据 + 自动连接/过期提醒）
// =============================================================
export interface ChannelCredentialField {
  key: string;
  label: string;
  type: 'text' | 'password' | 'textarea';
  required: boolean;
  placeholder?: string;
}
export interface ChannelPlatform {
  key: string;
  label: string;
  auth: string;
  connector: string;
  implemented: boolean;
  note: string;
  fields: ChannelCredentialField[];
}
export interface ChannelAccount {
  id: string;
  platform: string;
  platform_label: string;
  connector: string;
  adapter_implemented: boolean;
  name: string;
  enabled: boolean;
  credential: { configured: boolean; filled: string[]; missing: string[]; updated_at?: string | null };
  connection: string;
  status_label: string;
  reconnecting: boolean;
  expired: boolean;
  last_error: string;
  error_duration_sec?: number;
  created_at?: string | null;
}
export interface ChannelGroup extends ChannelPlatform {
  account_count: number;
  accounts: ChannelAccount[];
}
export interface ChannelAccountsResp {
  platforms: ChannelPlatform[];
  groups: ChannelGroup[];
  accounts: ChannelAccount[];
  total: number;
  daemon_alive: boolean;
  heartbeat_timeout_sec: number;
  expired_after_sec: number;
}

export async function listChannelAccounts(): Promise<ChannelAccountsResp> {
  return http('/admin/channels/accounts');
}
export async function createChannelAccount(
  platform: string, name: string, credentials?: Record<string, string>,
): Promise<{ ok: boolean; account: { id: string } }> {
  return http('/admin/channels/accounts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ platform, name, credentials }),
  });
}
export async function saveChannelCredentials(
  id: string, credentials: Record<string, string>,
): Promise<{ ok: boolean; daemon_alive: boolean; message: string }> {
  return http(`/admin/channels/accounts/${encodeURIComponent(id)}/credentials`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ credentials }),
  });
}
export async function channelAccountAction(
  id: string, action: 'start' | 'stop' | 'restart',
): Promise<{ ok: boolean }> {
  return http(`/admin/channels/accounts/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
}
export async function removeChannelAccount(id: string): Promise<{ ok: boolean }> {
  return http(`/admin/channels/accounts/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// =============================================================
// WP1 业务 KPI + 回答评价（CSAT）
// =============================================================
export interface KpiSnapshot {
  window: { start: string; end: string };
  sessions_total: number;
  messages_user: number;
  handoff: { sessions: number; tickets_total: number; p0_total: number };
  deflection_rate: number | null;
  handoff_rate: number | null;
  csat: number | null;
  ratings: { up: number; down: number; rated_sessions: number };
  rating_coverage: number | null;
  guard_blocked: number;
  guard_block_rate: number | null;
  avg_rounds: number | null;
  p0_ratio: number | null;
  cost: {
    input_tokens: number;
    output_tokens: number;
    total_cost_cny: number;
    avg_cost_per_session_cny: number | null;
    guard_savings_cny: number;
    pricing: { model: string; in_per_1k: number; out_per_1k: number };
    note: string;
  };
}

export interface KpiTrendResp {
  days: number;
  series: { date: string; sessions: number; handoffs: number; deflection_rate: number | null }[];
}

/** CSAT 评价：messageId 传 done 事件返回的 assistant 消息行 id（ 2.3 消息粒度）；
 *  0 = 会话粒度兜底（cache_hit 不落库 / 旧流程无 id） */
export async function rateAnswer(
  sessionId: string,
  rating: 'up' | 'down',
  comment = '',
  messageId = 0,
): Promise<{ ok: boolean }> {
  return http('/chat/rate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message_id: messageId, rating, comment }),
  });
}

export async function getAdminKpi(days = 14): Promise<KpiSnapshot> {
  return http(`/admin/analytics/kpi?days=${days}`);
}

export async function getKpiTrend(days = 14): Promise<KpiTrendResp> {
  return http(`/admin/analytics/kpi/trend?days=${days}`);
}


// =============================================================
// 店铺会话后台（实时会话 / 直发消息 / 接管）
// 路径与字段名沿用 xianyu，是历史技术标识（该通道以闲鱼 IM 协议实现），
// 对外展示统一叫「店铺 IM」——主场景是淘宝/抖音店铺客服。
// =============================================================

export interface XianyuConversation {
  channel: 'xianyu' | 'mall';
  cid: string;
  account?: string;
  session_id: string;
  name: string;
  msg_count: number;
  last_time: string | null;
  last_role: string | null;
  last_text: string | null;
  takeover?: boolean;
}

export interface XianyuMessage {
  id: number;
  role: string;
  content: string;
  create_time: string;
  sender: 'buyer' | 'assistant' | 'human';
  intent_snapshot?: Record<string, unknown>;
}

export interface XianyuEvent {
  ts: number;
  kind: string;
  data: Record<string, unknown>;
}

export async function listXianyuConversations(limit = 40): Promise<{ conversations: XianyuConversation[]; total: number }> {
  return http('/admin/xianyu/conversations?limit=' + limit);
}

export async function getXianyuMessages(channel: 'xianyu' | 'mall', cid: string, limit = 100): Promise<{ channel: string; cid: string; messages: XianyuMessage[] }> {
  return http('/admin/xianyu/conversations/' + channel + '/' + encodeURIComponent(cid) + '/messages?limit=' + limit);
}

export async function getXianyuEvents(limit = 50): Promise<{ events: XianyuEvent[] }> {
  return http('/admin/xianyu/events?limit=' + limit);
}

export async function sendXianyuMessage(cid: string, text: string, toid = '', channel = 'xianyu'): Promise<{ ok: boolean }> {
  return http('/admin/xianyu/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cid, text, toid, channel }),
  });
}

export async function takeoverXianyu(cid: string): Promise<{ ok: boolean }> {
  return http('/admin/xianyu/takeover', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cid }),
  });
}

export async function releaseXianyu(cid: string): Promise<{ ok: boolean }> {
  return http('/admin/xianyu/release', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cid }),
  });
}

export interface XianyuTicket {
  id: number;
  ticket_no: string;
  session_id: string;
  reason: string;
  priority: string;
  status: string;
  summary_text: string;
  create_time: string;
}

export async function getOpenTickets(): Promise<{ open_count: number; p0_count: number; tickets: XianyuTicket[] }> {
  return http('/admin/xianyu/tickets?limit=5');
}

export interface XianyuAccount {
  id: string;
  name: string;
  status: string;
  last_error?: string;
  started_at?: number;
}

export async function listAccounts(): Promise<{ accounts: XianyuAccount[]; total: number }> {
  return http('/admin/xianyu/accounts');
}

export async function accountAction(id: string, action: 'start' | 'stop' | 'restart' | 'reload'): Promise<{ ok: boolean }> {
  return http('/admin/xianyu/accounts/' + encodeURIComponent(id) + '/' + action, { method: 'POST' });
}

// ===== 质检（QA）：规则全量 + 抽样打分 =====
export interface QaDefect { code: string; count: number }
export interface QaStats {
  window_days: number;
  total: number;
  pass: number;
  low: number;
  blocked: number;
  pass_rate: number | null;
  low_rate: number | null;
  uncalibrated: number;
  pending_review: number;
  top_defects: QaDefect[];
  by_day: Record<string, unknown>[];
}
export interface QaItem {
  id: number;
  session_id: string;
  message_id: number;
  query: string;
  answer: string;
  intent: string | null;
  verdict: 'pass' | 'low' | 'blocked';
  source: 'rule' | 'llm';
  score: number | null;
  dims: Record<string, number> | null;
  rule_hits: string[] | null;
  failure_layer: string | null;
  calibrated: boolean;
  review_status: string;
  note: string | null;
  create_time: string | null;
}
export interface QaRunResp {
  batch_id: string;
  scanned: number;
  blocked: number;
  sampled: number;
  judged: number;
  low: number;
  calibrated: boolean | null;
}

export async function getQaStats(days = 7): Promise<QaStats> {
  return http(`/admin/qa/stats?days=${days}`);
}

export async function listQa(verdict?: string, limit = 20, offset = 0): Promise<{ total: number; items: QaItem[] }> {
  const q = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (verdict) q.set('verdict', verdict);
  return http(`/admin/qa/results?${q.toString()}`);
}

export async function runQa(payload: { since_hours?: number; ratio?: number; use_llm?: boolean; limit?: number }): Promise<QaRunResp> {
  return http('/admin/qa/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function qaToBadcase(id: number): Promise<{ qa_id: number; badcase: Record<string, unknown> }> {
  return http(`/admin/qa/${id}/to-badcase`, { method: 'POST' });
}
