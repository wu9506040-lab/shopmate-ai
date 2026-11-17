<script setup lang="ts">
/**
 * 人工介入坐席工作台（2026-11 调整）
 * 旧版只有一个「回复并结单」按钮：坐席回一句话工单立刻关闭，
 * 既不能多轮沟通，也没有用户超时机制；页面配色浅、状态看不清。
 * 现在：
 * - 「发送回复」保持工单处理中，可多轮沟通；「处理完成，结单」才显式结束
 * - 用户 2 分钟未回复由后端自动结单，页面显示倒计时
 * - 转人工原因随工单落库：中文标签 + 触发依据 + 用户原话，显著展示
 * - 建单规则：用户明确要人工 / AI 异常 / 业务规则（含发货后改址），按优先级分级
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  closeHandoffTicket,
  getHandoffTicket,
  getHandoffTickets,
  resolveHandoffTicket,
  sendHandoffReply,
  takeHandoffTicket,
  type HandoffTicketBrief,
  type HandoffTicketDetail,
} from '../api';

const statusTab = ref<'pending' | 'taken' | 'resolved' | 'closed'>('pending');
const tickets = ref<HandoffTicketBrief[]>([]);
const total = ref(0);
const current = ref<HandoffTicketDetail | null>(null);
const reply = ref('');
const busy = ref(false);
const error = ref('');
const notice = ref('');
let timer: number | undefined;

const TABS = [
  { key: 'pending', label: '待接' },
  { key: 'taken', label: '处理中' },
  { key: 'resolved', label: '已结单' },
  { key: 'closed', label: '已关闭' },
] as const;

const STATUS_LABEL: Record<string, string> = {
  pending: '待接', taken: '处理中', resolved: '已结单', closed: '已关闭',
};

async function refreshList(): Promise<void> {
  try {
    const resp = await getHandoffTickets(statusTab.value);
    tickets.value = resp.items;
    total.value = resp.total;
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  }
}

async function openTicket(id: number, keepNotice = false): Promise<void> {
  try {
    current.value = await getHandoffTicket(id);
    reply.value = '';
    error.value = '';
    if (!keepNotice) notice.value = '';
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  }
}

async function act(fn: () => Promise<unknown>, msg: string): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  notice.value = '';
  try {
    await fn();
    notice.value = msg;
    if (current.value) await openTicket(current.value.id, true);
    await refreshList();
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  } finally {
    busy.value = false;
  }
}

function take(): void {
  if (!current.value) return;
  void act(() => takeHandoffTicket(current.value!.id), '已认领，工单进入「处理中」');
}
function sendReply(): void {
  if (!current.value || !reply.value.trim()) return;
  const text = reply.value.trim();
  void act(() => sendHandoffReply(current.value!.id, text),
    '回复已发送，工单仍在处理中，可继续沟通或点「处理完成，结单」');
}
function resolve(): void {
  if (!current.value) return;
  const text = reply.value.trim() || undefined;
  void act(() => resolveHandoffTicket(current.value!.id, text), '工单已结单');
}
function close(): void {
  if (!current.value) return;
  void act(() => closeHandoffTicket(current.value!.id), '工单已关闭归档');
}

function priorityClass(p?: string | null): string {
  return p === 'P0' ? 'pri pri-p0' : p === 'P1' ? 'pri pri-p1' : 'pri';
}

/**
 * 用户原话：会话里用户消息的最后 3 条。
 * 坐席第一眼要看到「买家到底说了什么」，而不是只看分类标签——
 * 曾有工单因为 AI 回复里出现「质量问题」四个字被误判成 P0，界面上却看不到用户原话，无从判断真假。
 */
const userQuotes = computed<{ id: number; content: string; create_time?: string | null }[]>(() => {
  const msgs = current.value?.messages || [];
  return msgs.filter((m) => m.role === 'user').slice(-3);
});

/** 触发词是否真的出现在用户说的话里；不在 → 大概率是 AI 回复内容误触发，界面需显式告警 */
const kwMismatch = computed<boolean>(() => {
  const kw = current.value?.matched_keyword;
  if (!kw) return false;
  const userText = (current.value?.messages || [])
    .filter((m) => m.role === 'user')
    .map((m) => m.content)
    .join('\n');
  return !userText.includes(kw);
});
function statusClass(s?: string): string {
  return `st st-${s || 'pending'}`;
}
function timeText(s?: string | null): string {
  return s ? s.slice(0, 16).replace('T', ' ') : '—';
}

onMounted(() => {
  void refreshList();
  // 5s 轮询：刷新列表 + 当前工单（自动结单倒计时、用户新消息）
  timer = window.setInterval(() => {
    void refreshList();
    if (current.value) void openTicket(current.value.id, true);
  }, 5000);
});
onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer);
});
</script>

<template>
  <div class="workbench">
    <header class="page-head">
      <div>
        <h1>人工介入工作台</h1>
        <p>仅受理「用户明确要人工 / AI 异常 / 业务规则需人工」的工单，按优先级排队</p>
      </div>
      <div class="head-tools">
        <div class="tabs">
          <button v-for="t in TABS" :key="t.key" :class="{ active: statusTab === t.key }"
                  @click="statusTab = t.key; current = null; refreshList()">{{ t.label }}</button>
        </div>
        <span class="total">共 {{ total }} 张</span>
      </div>
    </header>

    <div v-if="error" class="alert error">{{ error }}</div>
    <div v-if="notice" class="alert ok">{{ notice }}</div>

    <div class="wb-body">
      <aside class="queue">
        <div class="queue-title">工单队列</div>
        <div v-for="t in tickets" :key="t.id" class="ticket"
             :class="{ cur: current?.id === t.id }" @click="openTicket(t.id)">
          <div class="row1">
            <span :class="priorityClass(t.priority)">{{ t.priority }}</span>
            <b>{{ t.ticket_no }}</b>
            <span :class="statusClass(t.status)">{{ STATUS_LABEL[t.status] || t.status }}</span>
          </div>
          <div class="reason-line">{{ t.reason_label || '转人工' }}<span v-if="t.category"> · {{ t.category }}</span></div>
          <div class="row3">用户 #{{ t.user_id }} · {{ timeText(t.create_time) }}
            <span v-if="t.assignee"> · {{ t.assignee }}</span>
            <span v-if="t.auto_closed" class="auto"> · 自动结单</span>
          </div>
        </div>
        <p v-if="!tickets.length" class="empty">队列为空</p>
      </aside>

      <section v-if="current" class="detail">
        <!-- 转人工原因：坐席第一眼要看到的信息 -->
        <div class="reason-card">
          <div class="reason-head">
            <span class="reason-kicker">为什么转人工</span>
            <b>{{ current.reason_label || current.reason }}</b>
            <span :class="priorityClass(current.priority)">{{ current.priority }}</span>
            <span :class="statusClass(current.status)">{{ STATUS_LABEL[current.status] || current.status }}</span>
          </div>
          <p class="reason-detail">{{ current.reason_detail || '该工单创建于早期版本，未记录详细原因；请结合下方会话上下文判断。' }}</p>
          <div class="reason-meta">
            <span v-if="current.matched_keyword">触发依据：<b>{{ current.matched_keyword }}</b></span>
            <span v-if="current.category">分类：<b>{{ current.category }}</b></span>
            <span>工单号：<b>{{ current.ticket_no }}</b></span>
            <span>创建：{{ timeText(current.create_time) }}</span>
          </div>
        </div>

        <!-- 用户原话：先看买家说了什么，再判断这个工单该不该存在 -->
        <div class="quotes">
          <div class="quotes-head">
            <span class="quotes-kicker">用户原话</span>
            <span class="hint">买家最后 {{ userQuotes.length }} 句</span>
            <span v-if="kwMismatch" class="warn">
              ⚠ 触发依据「{{ current.matched_keyword }}」未出现在用户消息中，疑似由 AI 回复内容误触发
            </span>
          </div>
          <ul v-if="userQuotes.length" class="quote-list">
            <li v-for="q in userQuotes" :key="q.id">
              <time>{{ timeText(q.create_time) }}</time>
              <span>{{ q.content }}</span>
            </li>
          </ul>
          <p v-else class="empty">该工单未关联到用户消息，无法核对诉求原文</p>
        </div>

        <div class="d-head">
          <b>会话上下文</b>
          <span class="hint">{{ current.messages.length }} 条消息</span>
          <span v-if="current.auto_close_remaining_sec !== null && current.auto_close_remaining_sec !== undefined && current.status === 'taken'"
                class="countdown">
            用户 {{ current.auto_close_remaining_sec }}s 内未回复将自动结单
          </span>
        </div>

        <div class="msgs">
          <div v-for="m in current.messages" :key="m.id"
               class="msg" :class="[m.role, { agent: m.is_human_agent }]">
            <div class="msg-meta">
              <span v-if="m.is_human_agent" class="badge">人工客服</span>
              <span v-else>{{ m.role === 'user' ? '用户' : 'AI 客服' }}</span>
              <time>{{ timeText(m.create_time) }}</time>
            </div>
            <div class="msg-text">{{ m.content }}</div>
          </div>
          <p v-if="!current.messages.length" class="empty">（该工单未关联会话或无历史）</p>
        </div>

        <template v-if="current.status !== 'resolved' && current.status !== 'closed'">
          <textarea v-model="reply" rows="3"
            placeholder="输入人工回复…发送后工单保持「处理中」，可继续多轮沟通"></textarea>
          <div class="ops">
            <button v-if="current.status === 'pending'" class="btn-primary" :disabled="busy" @click="take">
              认领工单
            </button>
            <template v-else>
              <button class="btn-primary" :disabled="busy || !reply.trim()" @click="sendReply">
                发送回复
              </button>
              <button class="btn-done" :disabled="busy" @click="resolve">
                处理完成，结单
              </button>
              <button class="btn-ghost" :disabled="busy" @click="close">直接关闭</button>
            </template>
          </div>
          <p class="hint">
            回复不会关单；处理完请点「处理完成，结单」。坐席回复后若用户 2 分钟无新消息，系统自动结单。
          </p>
        </template>
        <div v-else class="closed-note">
          该工单已{{ current.auto_closed ? '因用户超时未回复自动结单' : '结单' }}
          <span v-if="current.reply"> · 最后回复：{{ current.reply }}</span>
        </div>
      </section>
      <section v-else class="detail placeholder">← 选择左侧工单查看转人工原因与会话上下文</section>
    </div>
  </div>
</template>

<style scoped>
.workbench { display: flex; flex-direction: column; gap: 14px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 12px; }
.page-head h1 { font-size: 20px; font-weight: 800; color: #16202f; margin: 0 0 4px; }
.page-head p { font-size: 12px; color: #5b6879; margin: 0; }
.head-tools { display: flex; align-items: center; gap: 10px; }
.tabs { display: flex; gap: 5px; }
.tabs button {
  border: 1px solid #d6dde7; background: #fff; border-radius: 7px;
  padding: 6px 12px; font-size: 12px; color: #3d4a5c;
}
.tabs button.active { background: #1d4ed8; color: #fff; border-color: #1d4ed8; font-weight: 600; }
.total { font-size: 12px; color: #5b6879; }
.alert { border-radius: 8px; padding: 9px 13px; font-size: 12px; }
.alert.error { background: #fef3f2; border: 1px solid #fecdca; color: #b42318; }
.alert.ok { background: #ecfdf5; border: 1px solid #a7f3d0; color: #047857; }

.wb-body { display: flex; gap: 14px; min-height: 480px; align-items: flex-start; }
.queue {
  width: 310px; flex-shrink: 0; background: #fff; border: 1px solid #dfe4ec;
  border-radius: 10px; padding: 8px; box-shadow: 0 2px 8px rgba(15,23,42,.05);
  max-height: 620px; overflow-y: auto;
}
.queue-title { font-size: 12px; font-weight: 700; color: #4b5b70; padding: 6px 8px 10px; }
.ticket { padding: 10px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; margin-bottom: 4px; }
.ticket:hover { background: #f7f9fc; }
.ticket.cur { background: #eff6ff; border-color: #bfdbfe; }
.row1 { display: flex; gap: 7px; align-items: center; }
.row1 b { font-size: 12px; color: #16202f; }
.reason-line { font-size: 12px; color: #33445a; margin-top: 6px; font-weight: 600; }
.row3 { font-size: 11px; color: #7b8a9e; margin-top: 5px; }
.row3 .auto { color: #b45309; }
.pri { font-size: 10px; padding: 2px 6px; border-radius: 4px; background: #eef2f7; color: #5b6879; font-weight: 700; }
.pri-p0 { background: #fee2e2; color: #b91c1c; }
.pri-p1 { background: #fef3c7; color: #92400e; }
.st { font-size: 10px; padding: 2px 6px; border-radius: 4px; margin-left: auto; font-weight: 600; }
.st-pending { background: #fff7ed; color: #b45309; }
.st-taken { background: #eff6ff; color: #1d4ed8; }
.st-resolved { background: #ecfdf5; color: #047857; }
.st-closed { background: #f1f5f9; color: #64748b; }

.detail {
  flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 10px;
  background: #fff; border: 1px solid #dfe4ec; border-radius: 10px; padding: 14px 16px;
  box-shadow: 0 2px 8px rgba(15,23,42,.05);
}
.detail.placeholder { align-items: center; justify-content: center; color: #7b8a9e; font-size: 13px; min-height: 420px; }

.reason-card {
  background: #fff8f1; border: 1px solid #f3ddc4; border-left: 3px solid #d97706;
  border-radius: 8px; padding: 11px 14px;
}
.reason-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.reason-kicker { font-size: 10px; font-weight: 700; color: #92400e; letter-spacing: 1px; }
.reason-head b { font-size: 14px; color: #7c2d12; }
.reason-detail { font-size: 12px; color: #5b4636; line-height: 1.65; margin: 7px 0; }
.reason-meta { display: flex; flex-wrap: wrap; gap: 14px; font-size: 11px; color: #8a6a4a; }
.reason-meta b { color: #5b4636; }

/* 用户原话：与橙色「为什么转人工」区分开，蓝色系表示"原始证据" */
.quotes {
  background: #f7f9fc; border: 1px solid #e2e8f2; border-left: 3px solid #2563eb;
  border-radius: 8px; padding: 10px 14px;
}
.quotes-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.quotes-kicker { font-size: 10px; font-weight: 700; color: #1e40af; letter-spacing: 1px; }
.quotes-head .warn {
  font-size: 11px; color: #92400e; background: #fef3c7;
  border: 1px solid #fcd34d; border-radius: 5px; padding: 2px 7px;
}
.quote-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.quote-list li { display: flex; gap: 9px; font-size: 13px; color: #1b2536; line-height: 1.6; }
.quote-list time { flex: none; font-size: 11px; color: #8593a6; padding-top: 3px; }
.quote-list li span { font-weight: 500; }

.d-head { display: flex; align-items: center; gap: 10px; font-size: 13px; color: #223047; }
.hint { font-size: 11px; color: #7b8a9e; line-height: 1.6; }
.countdown {
  margin-left: auto; font-size: 11px; font-weight: 600; color: #b45309;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: 999px; padding: 3px 10px;
}
.msgs {
  flex: 1; overflow-y: auto; max-height: 340px; min-height: 180px;
  border: 1px solid #e8ecf3; border-radius: 8px; padding: 10px; background: #fafbfd;
}
.msg { margin: 8px 0; padding: 8px 11px; border-radius: 8px; max-width: 88%; }
.msg.user { background: #e8f0ff; margin-left: auto; }
.msg.assistant { background: #fff; border: 1px solid #e8ecf3; }
.msg.agent { background: #fff7e6; border: 1px solid #f2d9a8; }
.msg-meta { font-size: 10px; color: #7b8a9e; display: flex; gap: 8px; margin-bottom: 4px; }
.msg-meta time { margin-left: auto; }
.badge { color: #b26a00; font-weight: 700; }
.msg-text { font-size: 13px; color: #223047; white-space: pre-wrap; word-break: break-word; line-height: 1.6; }

textarea {
  border: 1px solid #d6dde7; border-radius: 8px; padding: 9px;
  font-size: 13px; color: #223047; resize: vertical; font-family: inherit;
}
.ops { display: flex; gap: 8px; flex-wrap: wrap; }
.ops button {
  height: 34px; padding: 0 15px; border-radius: 7px; font-size: 12px;
  font-weight: 600; border: 1px solid transparent;
}
.btn-primary { background: #1d4ed8; color: #fff; }
.btn-primary:hover:not(:disabled) { background: #1e40af; }
.btn-done { background: #047857; color: #fff; }
.btn-done:hover:not(:disabled) { background: #065f46; }
.btn-ghost { background: #fff; color: #3d4a5c; border-color: #d6dde7; }
.btn-ghost:hover:not(:disabled) { border-color: #9fb0c6; }
.ops button:disabled { opacity: .5; cursor: not-allowed; }
.closed-note {
  background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 8px;
  padding: 12px; font-size: 12px; color: #475569;
}
.empty { color: #7b8a9e; text-align: center; font-size: 12px; padding: 20px 0; }
</style>
