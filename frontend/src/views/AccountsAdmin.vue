<script setup lang="ts">
/**
 * 多渠道账号管理（2026-11 调整）
 * 旧版只列两个闲鱼账号 + 启停按钮，且 daemon 挂掉后 Redis 残留「运行中」，
 * 按钮只往 Redis 发命令就返回 ok —— 看起来成功、实际什么都没发生。
 * 现在：
 * - 按平台分组展示（闲鱼 / 淘宝 / 京东 / 抖音电商 / 自建商城），可自行新增账号
 * - 每个平台声明需要什么凭据；管理员在页面填写，保存即自动重载连接
 * - 闲鱼 Cookie 会过期：掉线先自动重连，持续失败标红提醒重新填写
 * - daemon 离线时明确显示，不再假报「运行中」
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  channelAccountAction,
  createChannelAccount,
  listChannelAccounts,
  removeChannelAccount,
  saveChannelCredentials,
  type ChannelAccount,
  type ChannelAccountsResp,
  type ChannelPlatform,
} from '../api';

const data = ref<ChannelAccountsResp | null>(null);
const error = ref('');
const notice = ref('');
const busy = ref('');
const polling = ref<number | undefined>(undefined);

// —— 新增账号表单 ——
const showAdd = ref(false);
const newPlatform = ref('xianyu');
const newName = ref('');
const newCreds = ref<Record<string, string>>({});

// —— 编辑凭据（按账号展开）——
const editingId = ref('');
const editCreds = ref<Record<string, string>>({});

const platforms = computed<ChannelPlatform[]>(() => data.value?.platforms || []);
const groups = computed(() => data.value?.groups || []);
const expiredAccounts = computed(() =>
  (data.value?.accounts || []).filter((a) => a.expired),
);
const currentPlatform = computed(() =>
  platforms.value.find((p) => p.key === newPlatform.value) || platforms.value[0],
);

function fieldsOf(platformKey: string) {
  return platforms.value.find((p) => p.key === platformKey)?.fields || [];
}

async function refresh(silent = false) {
  try {
    data.value = await listChannelAccounts();
    if (!silent) error.value = '';
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  }
}

function resetAdd() {
  showAdd.value = false;
  newName.value = '';
  newCreds.value = {};
}

async function addAccount() {
  if (!newName.value.trim()) {
    error.value = '请填写账号名称';
    return;
  }
  const f = currentPlatform.value;
  const required = f.fields.filter((x) => x.required);
  const missing = required.filter((x) => !newCreds.value[x.key]?.trim());
  if (missing.length && f.implemented) {
    error.value = `请填写：${missing.map((x) => x.label).join('、')}`;
    return;
  }
  busy.value = 'add';
  error.value = '';
  try {
    const creds = Object.fromEntries(
      Object.entries(newCreds.value).filter(([, v]) => String(v).trim()),
    );
    const r = await createChannelAccount(
      f.key, newName.value.trim(), Object.keys(creds).length ? creds : undefined,
    );
    notice.value = f.implemented && Object.keys(creds).length
      ? '账号已创建并触发自动连接，连接状态会在几秒内刷新'
      : `账号已创建（${f.label} 适配器待接入，凭据已保存）`;
    resetAdd();
    await refresh(true);
    void r;
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  } finally {
    busy.value = '';
  }
}

function openEdit(a: ChannelAccount) {
  editingId.value = editingId.value === a.id ? '' : a.id;
  editCreds.value = {};
  notice.value = '';
}

async function saveCreds(a: ChannelAccount) {
  const required = fieldsOf(a.platform).filter((f) => f.required);
  const missing = required.filter((f) => !editCreds.value[f.key]?.trim());
  if (missing.length) {
    error.value = `请填写：${missing.map((f) => f.label).join('、')}`;
    return;
  }
  busy.value = a.id;
  error.value = '';
  notice.value = '';
  try {
    const r = await saveChannelCredentials(a.id, editCreds.value);
    notice.value = r.message;
    editingId.value = '';
    editCreds.value = {};
    await refresh(true);
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  } finally {
    busy.value = '';
  }
}

async function action(a: ChannelAccount, act: 'start' | 'stop' | 'restart') {
  busy.value = a.id;
  error.value = '';
  try {
    await channelAccountAction(a.id, act);
    notice.value = `已下发 ${act} 命令，状态稍后刷新`;
    setTimeout(() => void refresh(true), 2000);
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  } finally {
    busy.value = '';
  }
}

async function remove(a: ChannelAccount) {
  if (!window.confirm(`确认移除账号「${a.name}」？凭据文件会一并删除。`)) return;
  busy.value = a.id;
  try {
    await removeChannelAccount(a.id);
    notice.value = '账号已移除';
    await refresh(true);
  } catch (e) {
    error.value = String(e instanceof Error ? e.message : e);
  } finally {
    busy.value = '';
  }
}

function connClass(a: ChannelAccount): string {
  if (a.expired) return 'expired';
  if (a.connection === 'online' || a.connection === 'internal') return 'online';
  if (a.reconnecting || a.connection === 'starting') return 'reconnecting';
  if (a.connection === 'pending_adapter') return 'pending';
  return 'offline';
}

onMounted(() => {
  void refresh();
  polling.value = window.setInterval(() => void refresh(true), 8000);
});
onBeforeUnmount(() => {
  if (polling.value) clearInterval(polling.value);
});
</script>

<template>
  <div class="acct">
    <header class="page-head">
      <div>
        <h1>账号管理</h1>
        <p>按平台管理店铺账号与连接凭据；闲鱼 Cookie 过期后可在此重新填写并自动重连</p>
      </div>
      <div class="head-tools">
        <span class="daemon" :class="{ off: !data?.daemon_alive }">
          <i></i>{{ data?.daemon_alive ? '连接服务运行中' : '连接服务未运行' }}
        </span>
        <button class="btn-ghost" @click="refresh()">刷新</button>
        <button class="btn-primary" @click="showAdd = !showAdd; notice = ''">
          {{ showAdd ? '取消新增' : '新增账号' }}
        </button>
      </div>
    </header>

    <div v-if="error" class="alert error">{{ error }}</div>
    <div v-if="notice" class="alert ok">{{ notice }}</div>
    <div v-if="expiredAccounts.length" class="alert expired">
      <b>{{ expiredAccounts.length }} 个账号凭据已过期</b>
      <span>自动重连持续失败，请重新粘贴 Cookie / 凭据后保存，系统会立即重连。</span>
    </div>

    <!-- 新增账号 -->
    <section v-if="showAdd" class="panel add-panel">
      <div class="panel-title">新增平台账号</div>
      <div class="add-grid">
        <label>
          <span>平台</span>
          <select v-model="newPlatform" @change="newCreds = {}">
            <option v-for="p in platforms" :key="p.key" :value="p.key">{{ p.label }}</option>
          </select>
        </label>
        <label>
          <span>账号名称</span>
          <input v-model="newName" placeholder="例如：猫与小鼠闲鱼主号" />
        </label>
      </div>
      <p class="platform-note">{{ currentPlatform?.auth }} · {{ currentPlatform?.note }}</p>
      <div v-if="currentPlatform?.fields.length" class="cred-grid">
        <label v-for="f in currentPlatform.fields" :key="f.key">
          <span>{{ f.label }}<em v-if="f.required">*</em></span>
          <textarea v-if="f.type === 'textarea'" v-model="newCreds[f.key]" rows="3"
                     :placeholder="f.placeholder || '粘贴完整 ' + f.label"></textarea>
          <input v-else v-model="newCreds[f.key]" :type="f.type === 'password' ? 'password' : 'text'"
                 :placeholder="f.placeholder || f.label" />
        </label>
      </div>
      <div class="panel-actions">
        <span class="hint">凭据仅保存在服务端，不会回显到页面</span>
        <button class="btn-primary" :disabled="busy === 'add'" @click="addAccount">
          {{ busy === 'add' ? '保存中…' : '添加并连接' }}
        </button>
      </div>
    </section>

    <!-- 平台分组 -->
    <section v-for="g in groups" :key="g.key" class="panel group">
      <div class="group-head">
        <div>
          <span class="group-title">{{ g.label }}</span>
          <span class="group-count">{{ g.account_count }} 个账号</span>
          <span v-if="!g.implemented" class="tag pending">适配器待接入</span>
          <span v-else-if="g.connector === 'internal'" class="tag internal">内置通道</span>
          <span v-else class="tag ready">可连接</span>
        </div>
        <span class="group-auth">{{ g.auth }}</span>
      </div>
      <p class="group-note">{{ g.note }}</p>

      <div v-if="g.accounts.length" class="acc-list">
        <div v-for="a in g.accounts" :key="a.id" class="acc-row">
          <div class="acc-main">
            <div class="acc-title-line">
              <b>{{ a.name }}</b>
              <span class="acc-id">{{ a.id }}</span>
              <span class="conn" :class="connClass(a)"><i></i>{{ a.status_label }}</span>
            </div>
            <div class="acc-meta">
              <span :class="{ warn: !a.credential.configured }">
                凭据：{{ a.credential.configured ? '已配置' : '未配置' }}
                <template v-if="a.credential.updated_at"> · {{ a.credential.updated_at.slice(0, 16).replace('T', ' ') }}</template>
              </span>
              <span v-if="a.error_duration_sec"> · 已重连 {{ Math.round(a.error_duration_sec) }}s</span>
            </div>
            <div v-if="a.last_error" class="acc-err">{{ a.last_error }}</div>
          </div>

          <div class="acc-ops">
            <button class="btn-ghost" @click="openEdit(a)">
              {{ a.credential.configured ? '更新凭据' : '填写凭据' }}
            </button>
            <template v-if="g.implemented && g.connector !== 'internal'">
              <button class="btn-ghost" :disabled="busy === a.id || !data?.daemon_alive"
                      @click="action(a, 'restart')">重启</button>
              <button class="btn-ghost" :disabled="busy === a.id || !data?.daemon_alive"
                      @click="action(a, 'stop')">停止</button>
              <button class="btn-ghost" :disabled="busy === a.id || !data?.daemon_alive"
                      @click="action(a, 'start')">启动</button>
            </template>
            <button class="btn-danger" :disabled="busy === a.id" @click="remove(a)">移除</button>
          </div>

          <!-- 内联凭据表单 -->
          <div v-if="editingId === a.id" class="edit-box">
            <div class="cred-grid">
              <label v-for="f in fieldsOf(a.platform)" :key="f.key">
                <span>{{ f.label }}<em v-if="f.required">*</em></span>
                <textarea v-if="f.type === 'textarea'" v-model="editCreds[f.key]" rows="3"
                           :placeholder="f.placeholder || '重新粘贴完整 ' + f.label"></textarea>
                <input v-else v-model="editCreds[f.key]"
                       :type="f.type === 'password' ? 'password' : 'text'"
                       :placeholder="f.placeholder || f.label" />
              </label>
            </div>
            <p class="hint">出于安全考虑不回显已保存的凭据，更新时请重新完整填写。</p>
            <div class="panel-actions">
              <button class="btn-ghost" @click="editingId = ''">取消</button>
              <button class="btn-primary" :disabled="busy === a.id" @click="saveCreds(a)">
                {{ busy === a.id ? '保存中…' : '保存并自动连接' }}
              </button>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="empty">暂无账号，点右上角「新增账号」添加</div>
    </section>
  </div>
</template>

<style scoped>
.acct { display: flex; flex-direction: column; gap: 14px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap; }
.page-head h1 { font-size: 20px; font-weight: 800; color: #16202f; margin: 0 0 4px; }
.page-head p { font-size: 12px; color: #5b6879; margin: 0; }
.head-tools { display: flex; align-items: center; gap: 8px; }
.daemon {
  font-size: 11px; font-weight: 600; color: #0f766e; background: #e7f8f2;
  border: 1px solid #bfe9db; border-radius: 999px; padding: 5px 11px;
  display: inline-flex; align-items: center; gap: 6px;
}
.daemon i { width: 7px; height: 7px; border-radius: 50%; background: #10b981; }
.daemon.off { color: #b42318; background: #fef3f2; border-color: #fecdca; }
.daemon.off i { background: #ef4444; }

.btn-primary, .btn-ghost, .btn-danger {
  height: 32px; padding: 0 13px; border-radius: 7px; font-size: 12px;
  font-weight: 600; border: 1px solid transparent;
}
.btn-primary { background: #d4232f; color: #fff; border-color: #d4232f; }
.btn-primary:hover:not(:disabled) { background: #b31422; }
.btn-ghost { background: #fff; color: #3d4a5c; border-color: #d6dde7; }
.btn-ghost:hover:not(:disabled) { border-color: #9fb0c6; }
.btn-danger { background: #fff; color: #b42318; border-color: #f3c9c5; }
.btn-danger:hover:not(:disabled) { background: #fef3f2; }
button:disabled { opacity: .5; cursor: not-allowed; }

.alert { border-radius: 8px; padding: 10px 14px; font-size: 12px; line-height: 1.6; }
.alert.error { background: #fef3f2; border: 1px solid #fecdca; color: #b42318; }
.alert.ok { background: #ecfdf5; border: 1px solid #a7f3d0; color: #047857; }
.alert.expired {
  background: #fff7ed; border: 1px solid #fed7aa; color: #9a3412;
  display: flex; flex-direction: column; gap: 2px;
}

.panel {
  background: #fff; border: 1px solid #dfe4ec; border-radius: 10px; padding: 14px 16px;
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05);
}
.panel-title { font-size: 13px; font-weight: 700; color: #223047; margin-bottom: 10px; }
.add-grid, .cred-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #4b5b70; font-weight: 600; }
label em { color: #d4232f; font-style: normal; margin-left: 2px; }
input, select, textarea {
  border: 1px solid #d6dde7; border-radius: 7px; padding: 7px 9px;
  font-size: 12px; color: #223047; background: #fff; font-family: inherit;
}
textarea { resize: vertical; }
.platform-note, .group-note, .hint { font-size: 11px; color: #7b8a9e; line-height: 1.6; }
.platform-note { margin: 8px 0 10px; }
.panel-actions { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 12px; }
.panel-actions .hint { margin-right: auto; }

.group-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.group-title { font-size: 15px; font-weight: 800; color: #16202f; }
.group-count { margin-left: 8px; font-size: 12px; color: #7b8a9e; }
.group-auth { font-size: 11px; color: #5b6879; background: #f1f4f8; border-radius: 5px; padding: 3px 8px; }
.group-note { margin: 6px 0 12px; }
.tag {
  font-size: 10px; font-weight: 600; border-radius: 4px; padding: 2px 7px; margin-left: 6px;
  background: #eef2f7; color: #5b6879; border: 1px solid #dfe4ec;
}
.tag.ready { background: #ecfdf5; color: #047857; border-color: #a7f3d0; }
.tag.pending { background: #fffbeb; color: #92400e; border-color: #fde68a; }
.tag.internal { background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }

.acc-list { display: flex; flex-direction: column; }
.acc-row {
  border-top: 1px solid #eef1f6; padding: 12px 0;
  display: flex; flex-wrap: wrap; align-items: flex-start; gap: 10px;
}
.acc-main { flex: 1; min-width: 260px; }
.acc-title-line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.acc-title-line b { font-size: 13px; color: #16202f; }
.acc-id { font-size: 11px; color: #8a97a8; font-family: ui-monospace, monospace; }
.acc-meta { margin-top: 5px; font-size: 11px; color: #5b6879; }
.acc-meta .warn { color: #b45309; }
.acc-err {
  margin-top: 6px; font-size: 11px; color: #b42318; background: #fef3f2;
  border-radius: 5px; padding: 5px 8px; white-space: pre-wrap; word-break: break-all;
}
.conn { font-size: 11px; font-weight: 600; display: inline-flex; align-items: center; gap: 5px; }
.conn i { width: 7px; height: 7px; border-radius: 50%; background: #94a3b8; }
.conn.online { color: #047857; } .conn.online i { background: #10b981; }
.conn.reconnecting { color: #b45309; } .conn.reconnecting i { background: #f59e0b; }
.conn.expired { color: #b42318; } .conn.expired i { background: #ef4444; }
.conn.pending { color: #1d4ed8; } .conn.pending i { background: #3b82f6; }
.conn.offline { color: #64748b; }
.acc-ops { display: flex; flex-wrap: wrap; gap: 6px; }
.edit-box {
  width: 100%; background: #f8fafc; border: 1px solid #e3e8f0;
  border-radius: 8px; padding: 12px; margin-top: 6px;
}
.edit-box .hint { margin: 8px 0 0; }
.empty { padding: 20px; text-align: center; font-size: 12px; color: #7b8a9e; }
</style>
