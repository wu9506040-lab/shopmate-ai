<script setup lang="ts">
/**
 * 质检（QA）看板
 *
 * 只放 4 个数 + Top 缺陷 + 结果表：
 * 指标一多就没人看，运营真正会点开的是「哪条会话低分、原因是什么、要不要转 badcase」。
 * 口径写在页脚：分母只算 AI 真作答的轮次，转人工单独统计。
 */
import { computed, onMounted, ref } from 'vue';
import { getQaStats, listQa, qaToBadcase, runQa, type QaItem, type QaStats } from '../api';

const days = ref(7);
const stats = ref<QaStats | null>(null);
const items = ref<QaItem[]>([]);
const total = ref(0);
const verdict = ref<'' | 'low' | 'blocked'>('low');
const loading = ref(false);
const running = ref(false);
const error = ref('');
const toast = ref('');

function pct(v: number | null): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(1)}%`;
}

const dimLabel: Record<string, string> = {
  relevance: '答在点上',
  grounding: '依据可查',
  consistency: '口径一致',
  actionable: '给出下一步',
};

const cards = computed(() => {
  const s = stats.value;
  if (!s) return [];
  return [
    { label: '合格率', value: pct(s.pass_rate), sub: `合格 ${s.pass} / 计分 ${s.total - s.blocked}`, tone: 'green' },
    { label: '低分率', value: pct(s.low_rate), sub: `低分 ${s.low} 轮 · 待审 ${s.pending_review}`, tone: 'red' },
    { label: '规则拦截', value: String(s.blocked), sub: '空答/兜底/截断/无引用/承诺', tone: 'amber' },
    { label: '未校准批次', value: String(s.uncalibrated), sub: '坏答案拿高分则整批不可信', tone: 'slate' },
  ];
});

async function loadStats() {
  stats.value = await getQaStats(days.value);
}

async function loadList() {
  const res = await listQa(verdict.value || undefined, 20, 0);
  items.value = res.items;
  total.value = res.total;
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    await Promise.all([loadStats(), loadList()]);
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

async function onRun() {
  running.value = true;
  toast.value = '';
  error.value = '';
  try {
    const r = await runQa({ since_hours: 24, use_llm: true });
    toast.value = `扫描 ${r.scanned} 条 · 拦截 ${r.blocked} · 抽检 ${r.sampled} · 低分 ${r.low}` +
      (r.calibrated === false ? '（本批未通过校准，分数仅留档）' : '');
    await load();
  } catch (e) {
    error.value = e instanceof Error ? e.message : '跑批失败';
  } finally {
    running.value = false;
  }
}

async function onToBadcase(row: QaItem) {
  try {
    await qaToBadcase(row.id);
    row.review_status = 'accepted';
    toast.value = `已转入 badcase 池（会话 ${row.session_id.slice(0, 12)}）`;
    await loadStats();
  } catch (e) {
    error.value = e instanceof Error ? e.message : '转 badcase 失败';
  }
}

function short(s: string, n = 60): string {
  return s && s.length > n ? s.slice(0, n) + '…' : s;
}

onMounted(load);
</script>

<template>
  <div class="qa-page">
    <header class="head">
      <div>
        <h2>质检（QA）</h2>
        <p class="sub">规则层全量扫 + LLM 抽样打分；低分可一键转 badcase 池回流评测集。</p>
      </div>
      <div class="actions">
        <select v-model.number="days" @change="load">
          <option :value="7">近 7 天</option>
          <option :value="30">近 30 天</option>
          <option :value="90">近 90 天</option>
        </select>
        <button class="primary" :disabled="running" @click="onRun">
          {{ running ? '跑批中…' : '跑一轮质检' }}
        </button>
      </div>
    </header>

    <p v-if="error" class="alert err">{{ error }}</p>
    <p v-if="toast" class="alert ok">{{ toast }}</p>

    <section class="cards">
      <article v-for="c in cards" :key="c.label" class="card" :class="c.tone">
        <span class="label">{{ c.label }}</span>
        <strong>{{ c.value }}</strong>
        <span class="hint">{{ c.sub }}</span>
      </article>
    </section>

    <section class="panel">
      <h3>Top 缺陷</h3>
      <ul v-if="stats?.top_defects.length" class="defects">
        <li v-for="d in stats.top_defects" :key="d.code">
          <code>{{ d.code }}</code>
          <span class="bar"><i :style="{ width: Math.min(100, (d.count / (stats.top_defects[0]?.count || 1)) * 100) + '%' }" /></span>
          <b>{{ d.count }}</b>
        </li>
      </ul>
      <p v-else class="empty">还没有数据，点右上角「跑一轮质检」。</p>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>质检结果</h3>
        <div class="tabs">
          <button :class="{ on: verdict === 'low' }" @click="verdict = 'low'; loadList()">低分</button>
          <button :class="{ on: verdict === 'blocked' }" @click="verdict = 'blocked'; loadList()">规则拦截</button>
          <button :class="{ on: verdict === '' }" @click="verdict = ''; loadList()">全部</button>
          <span class="count">共 {{ total }} 条</span>
        </div>
      </div>

      <table v-if="items.length" class="tbl">
        <thead>
          <tr>
            <th>问句</th>
            <th>答案片段</th>
            <th>判定</th>
            <th>四维</th>
            <th>原因</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in items" :key="r.id">
            <td class="q">{{ short(r.query, 26) }}</td>
            <td class="a">{{ short(r.answer.replace(/\s+/g, ' '), 46) }}</td>
            <td>
              <span class="tag" :class="r.verdict">{{ r.verdict }}</span>
              <span v-if="!r.calibrated" class="tag warn">未校准</span>
            </td>
            <td class="dims">
              <template v-if="r.dims">
                <span v-for="(v, k) in r.dims" :key="k" :title="dimLabel[k] || k" :class="{ bad: v <= 2 }">{{ v }}</span>
              </template>
              <span v-else class="muted">—</span>
            </td>
            <td class="note">{{ short(r.note || (r.rule_hits || []).join(' / '), 30) }}</td>
            <td>
              <button v-if="r.verdict === 'low' && r.review_status !== 'accepted'" class="mini" @click="onToBadcase(r)">
                转 badcase
              </button>
              <span v-else-if="r.review_status === 'accepted'" class="muted">已入池</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!loading" class="empty">这个筛选下没有记录。</p>
    </section>

    <footer class="foot">
      口径：合格率分母只算 AI 真作答的轮次（转人工单独统计）；四维分数取最低维判定，
      3 分及以上判合格；未通过「坏答案校准」的批次只留档、不进人工队列。
    </footer>
  </div>
</template>

<style scoped>
.qa-page { padding: 20px 24px 40px; color: #1f2430; }
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
h2 { margin: 0; font-size: 20px; }
.sub { margin: 6px 0 0; color: #6b7280; font-size: 13px; }
.actions { display: flex; gap: 10px; align-items: center; }
select, button { font: inherit; font-size: 13px; border-radius: 8px; border: 1px solid #d7dbe3; background: #fff; color: #1f2430; padding: 7px 12px; cursor: pointer; }
button.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
button.primary:disabled { opacity: .6; cursor: default; }
.alert { margin: 12px 0 0; padding: 9px 12px; border-radius: 8px; font-size: 13px; }
.alert.err { background: #fdecec; color: #a12a2a; }
.alert.ok { background: #eaf5ee; color: #23663c; }
.cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin: 18px 0; }
.card { background: #fff; border: 1px solid #e6e9f0; border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 6px; }
.card .label { font-size: 12px; color: #6b7280; }
.card strong { font-size: 24px; font-weight: 600; }
.card .hint { font-size: 12px; color: #8a93a3; }
.card.green strong { color: #1f8a52; }
.card.red strong { color: #c0392b; }
.card.amber strong { color: #b06a12; }
.card.slate strong { color: #4b5563; }
.panel { background: #fff; border: 1px solid #e6e9f0; border-radius: 12px; padding: 16px 18px; margin-bottom: 16px; }
.panel h3 { margin: 0 0 12px; font-size: 15px; }
.defects { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.defects li { display: grid; grid-template-columns: 180px 1fr 48px; align-items: center; gap: 12px; font-size: 13px; }
.defects code { background: #f4f6fa; padding: 2px 8px; border-radius: 6px; color: #334155; }
.defects .bar { background: #f1f3f7; height: 8px; border-radius: 4px; overflow: hidden; }
.defects .bar i { display: block; height: 100%; background: #7aa7f5; }
.panel-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.tabs { display: flex; align-items: center; gap: 6px; }
.tabs button { padding: 5px 10px; }
.tabs button.on { background: #eef4ff; border-color: #b9d0fb; color: #1d4ed8; }
.tabs .count { color: #8a93a3; font-size: 12px; margin-left: 6px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th { text-align: left; color: #6b7280; font-weight: 500; padding: 8px 10px; border-bottom: 1px solid #eef0f5; }
.tbl td { padding: 9px 10px; border-bottom: 1px solid #f4f5f8; vertical-align: top; }
.tbl td.q { color: #111827; }
.tbl td.a, .tbl td.note { color: #6b7280; }
.dims { white-space: nowrap; }
.dims span { display: inline-block; min-width: 18px; text-align: center; margin-right: 4px; background: #eef7f1; color: #1f7a4c; border-radius: 5px; font-size: 12px; padding: 1px 4px; }
.dims span.bad { background: #fdeaea; color: #b42318; }
.tag { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #eef1f6; color: #475569; }
.tag.low { background: #fdeaea; color: #b42318; }
.tag.blocked { background: #fff3e0; color: #a2610c; }
.tag.pass { background: #eaf5ee; color: #23663c; }
.tag.warn { background: #f5f3ff; color: #6d28d9; margin-left: 4px; }
.tbl td:last-child { white-space: nowrap; }
.mini { padding: 4px 10px; font-size: 12px; white-space: nowrap; }
.muted { color: #9aa2b1; font-size: 12px; }
.empty { color: #8a93a3; font-size: 13px; margin: 8px 0 0; }
.foot { margin-top: 14px; color: #8a93a3; font-size: 12px; line-height: 1.7; }
@media (max-width: 900px) {
  .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
