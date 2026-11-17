<script setup lang="ts">
/**
 * 猫与小鼠 · 业务 KPI 大盘
 * 2026-11 展示调整（替换老式 980px 表格版）：
 * - 全宽自适应卡片 + 明确层次，解决「页面一片惨白、数值看不清」
 * - 趋势改 ECharts（会话量柱 + 自助解决率折线双轴），0 值日期也可见
 * - 空数据显式提示：近 14 天多数日期没有咨询，不再留一大片空白
 * - 主文字用高对比深灰，数值给品牌色，次级说明不再用 #99a 这类浅灰
 * 数据源：GET /api/admin/analytics/kpi 与 /kpi/trend（require_admin）
 */
import * as echarts from 'echarts';
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { getAdminKpi, getKpiTrend, type KpiSnapshot, type KpiTrendResp } from '../api';

const days = ref(14);
const kpi = ref<KpiSnapshot | null>(null);
const trend = ref<KpiTrendResp | null>(null);
const loading = ref(false);
const error = ref('');
const charts: echarts.ECharts[] = [];

function pct(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`;
}
function fixed(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? '—' : Number(v).toFixed(digits);
}
function money(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `¥${Number(v).toFixed(4)}`;
}

interface Card {
  label: string;
  value: string;
  sub: string;
  tone: 'red' | 'blue' | 'green' | 'amber' | 'slate' | 'violet';
}
const cards = computed<Card[]>(() => {
  const k = kpi.value;
  if (!k) return [];
  return [
    {
      label: 'AI 自助解决率',
      value: pct(k.deflection_rate),
      sub: `未转人工会话 / 总会话 ${k.sessions_total}`,
      tone: 'green',
    },
    {
      label: '转人工率',
      value: pct(k.handoff_rate),
      sub: `工单 ${k.handoff.tickets_total} 张 · P0 ${k.handoff.p0_total} 张`,
      tone: 'red',
    },
    {
      label: '平均解决轮次',
      value: fixed(k.avg_rounds, 2),
      sub: `用户消息 ${k.messages_user} 条`,
      tone: 'blue',
    },
    {
      label: 'CSAT 满意度',
      value: pct(k.csat, 0),
      sub: `👍 ${k.ratings.up} / 👎 ${k.ratings.down} · 覆盖率 ${pct(k.rating_coverage, 0)}`,
      tone: 'amber',
    },
    {
      label: 'Guard 拦截率',
      value: pct(k.guard_block_rate),
      sub: `拦截 ${k.guard_blocked} 次 · 零 LLM Token 消耗`,
      tone: 'violet',
    },
    {
      label: 'LLM 成本（估算）',
      value: money(k.cost.total_cost_cny),
      sub: `in ${k.cost.input_tokens} / out ${k.cost.output_tokens} tok · ${k.cost.pricing.model}`,
      tone: 'slate',
    },
  ];
});

/** 有咨询数据的日期（全 0 行不进表格，避免"一堆空行"） */
const activeDays = computed(() =>
  (trend.value?.series || []).filter((d) => d.sessions > 0 || d.handoffs > 0),
);
const hasTrend = computed(() => (trend.value?.series || []).length > 0);

function mount(id: string, option: echarts.EChartsCoreOption) {
  const el = document.getElementById(id);
  if (!el) return;
  const c = echarts.init(el, undefined, { renderer: 'canvas' });
  c.setOption(option);
  charts.push(c);
}

function initCharts() {
  for (const c of charts) c.dispose();
  charts.length = 0;
  if (!trend.value || !hasTrend.value) return;

  const s = trend.value.series;
  const dates = s.map((d) => d.date);
  const sessions = s.map((d) => d.sessions);
  const handoffs = s.map((d) => d.handoffs);
  const deflection = s.map((d) =>
    d.deflection_rate === null ? null : Number((d.deflection_rate * 100).toFixed(1)),
  );

  const baseText = { color: '#4b5b70', fontSize: 11 };
  const splitLine = { lineStyle: { color: '#e8edf4', type: 'dashed' as const } };

  // 主图：会话量（柱）+ 转人工工单（柱）+ 自助解决率（折线，右轴）
  mount('kpi-trend', {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 4, textStyle: baseText, itemWidth: 14, itemHeight: 9 },
    grid: { left: 46, right: 52, top: 42, bottom: 30 },
    xAxis: {
      type: 'category', data: dates,
      axisLabel: { ...baseText, fontSize: 10 },
      axisLine: { lineStyle: { color: '#cbd5e1' } },
      axisTick: { show: false },
    },
    yAxis: [
      { type: 'value', name: '会话 / 工单', nameTextStyle: baseText, axisLabel: baseText, splitLine },
      {
        type: 'value', name: '自助率 %', min: 0, max: 100,
        nameTextStyle: baseText, axisLabel: { ...baseText, formatter: '{value}%' }, splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '会话量', type: 'bar', barMaxWidth: 22, yAxisIndex: 0,
        itemStyle: { color: '#2563eb', borderRadius: [3, 3, 0, 0] }, data: sessions,
      },
      {
        name: '转人工工单', type: 'bar', barMaxWidth: 22, yAxisIndex: 0,
        itemStyle: { color: '#f0a4a9', borderRadius: [3, 3, 0, 0] }, data: handoffs,
      },
      {
        name: '自助解决率', type: 'line', yAxisIndex: 1, smooth: true, connectNulls: false,
        symbolSize: 6, lineStyle: { width: 2.5, color: '#d4232f' },
        itemStyle: { color: '#d4232f' }, data: deflection,
      },
    ],
  });

  // 副图：服务结构（自助解决 vs 转人工），比"再来一张空折线"更有信息量
  const k = kpi.value;
  const selfServed = k ? Math.max(0, k.sessions_total - k.handoff.sessions) : 0;
  mount('kpi-split', {
    tooltip: { trigger: 'item', formatter: '{b}: {c} 会话 ({d}%)' },
    legend: { bottom: 2, textStyle: baseText, itemWidth: 12, itemHeight: 9 },
    series: [{
      type: 'pie', radius: ['46%', '70%'], center: ['50%', '44%'],
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      label: { formatter: '{b}\n{d}%', fontSize: 11, color: '#33445a' },
      data: [
        { name: 'AI 自助解决', value: selfServed, itemStyle: { color: '#10b981' } },
        { name: '转人工', value: k?.handoff.sessions ?? 0, itemStyle: { color: '#d4232f' } },
      ],
    }],
  });
}

function onResize() {
  for (const c of charts) c.resize();
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const [k, t] = await Promise.all([getAdminKpi(days.value), getKpiTrend(days.value)]);
    kpi.value = k;
    trend.value = t;
    await nextTick(); // 图表容器由 v-if 渲染，必须等 DOM 就绪再 init
    initCharts();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  load();
  window.addEventListener('resize', onResize);
});
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize);
  for (const c of charts) c.dispose();
  charts.length = 0;
});
</script>

<template>
  <div class="kpi-page">
    <header class="page-head">
      <div>
        <h1>业务 KPI 大盘</h1>
        <p>服务效率与成本概览 · 数据来自真实会话与工单</p>
      </div>
      <div class="head-tools">
        <span class="status" :class="{ err: !!error }">{{ error ? '接口异常' : loading ? '加载中' : '真实统计' }}</span>
        <select v-model.number="days" @change="load">
          <option :value="7">近 7 天</option>
          <option :value="14">近 14 天</option>
          <option :value="30">近 30 天</option>
        </select>
        <button class="refresh" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
      </div>
    </header>

    <p v-if="error" class="error-banner">{{ error }}</p>

    <template v-if="kpi">
      <section class="cards">
        <div v-for="c in cards" :key="c.label" class="card" :class="`tone-${c.tone}`">
          <div class="card-label">{{ c.label }}</div>
          <div class="card-value">{{ c.value }}</div>
          <div class="card-sub">{{ c.sub }}</div>
        </div>
      </section>

      <section class="chart-row">
        <div class="panel chart-main">
          <div class="panel-title">按日趋势 · 会话量 / 转人工 / 自助解决率</div>
          <div id="kpi-trend" class="chart h300"></div>
          <div v-if="activeDays.length === 0" class="empty-hint">
            近 {{ days }} 天暂无咨询数据，图表为空属正常状态
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">服务结构</div>
          <div id="kpi-split" class="chart h300"></div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-title">
          明细（仅有咨询的日期）
          <span class="panel-note">共 {{ activeDays.length }} / {{ trend?.series.length ?? 0 }} 天有数据</span>
        </div>
        <div v-if="activeDays.length" class="table-wrap">
          <table>
            <thead>
              <tr><th>日期</th><th>会话数</th><th>转人工</th><th>自助解决率</th><th class="bar-col">占比</th></tr>
            </thead>
            <tbody>
              <tr v-for="d in activeDays" :key="d.date">
                <td class="date">{{ d.date }}</td>
                <td>{{ d.sessions }}</td>
                <td>{{ d.handoffs }}</td>
                <td>{{ pct(d.deflection_rate) }}</td>
                <td class="bar-col">
                  <div class="bar"><div class="fill" :style="{ width: `${(d.deflection_rate ?? 0) * 100}%` }"></div></div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-block">该时间窗内没有产生咨询会话</div>
      </section>

      <section class="notes">
        <b>口径说明</b>
        <span>自助解决率 = 未转人工会话 / 总会话；转人工率 = 工单数 / 会话数；CSAT 仅在有评价的会话内计算，覆盖率低时不作趋势结论。</span>
        <span>成本按 {{ kpi.cost.pricing.model }} 计价估算（in {{ kpi.cost.pricing.in_per_1k }} / out {{ kpi.cost.pricing.out_per_1k }} 每 1k token）；Guard 拦截省钱估算 {{ money(kpi.cost.guard_savings_cny) }}。</span>
      </section>
    </template>

    <div v-else-if="!loading && !error" class="empty-block">暂无 KPI 数据</div>
  </div>
</template>

<style scoped>
.kpi-page { display: flex; flex-direction: column; gap: 16px; }

.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap; }
.page-head h1 { font-size: 20px; font-weight: 800; color: #16202f; margin: 0 0 4px; }
.page-head p { font-size: 12px; color: #5b6879; margin: 0; }
.head-tools { display: flex; align-items: center; gap: 8px; }
.status {
  font-size: 11px; font-weight: 600; color: #0f766e; background: #e7f8f2;
  border: 1px solid #bfe9db; border-radius: 999px; padding: 4px 10px;
}
.status.err { color: #b42318; background: #fef3f2; border-color: #fecdca; }
.head-tools select, .refresh {
  height: 32px; padding: 0 10px; border: 1px solid #d6dde7; border-radius: 7px;
  background: #fff; color: #33445a; font-size: 12px;
}
.refresh { background: #d4232f; color: #fff; border-color: #d4232f; font-weight: 600; padding: 0 14px; }
.refresh:hover:not(:disabled) { background: #b31422; }
.refresh:disabled { opacity: 0.6; cursor: not-allowed; }
.error-banner {
  background: #fef3f2; border: 1px solid #fecdca; color: #b42318;
  border-radius: 8px; padding: 10px 14px; font-size: 12px;
}

/* KPI 卡：白底 + 明显边框阴影 + 顶部色条，避免"一片白" */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(172px, 1fr)); gap: 12px; }
.card {
  background: #fff; border: 1px solid #dfe4ec; border-radius: 10px;
  padding: 15px 16px 14px; position: relative; overflow: hidden;
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.06);
}
.card::before { content: ''; position: absolute; left: 0; top: 0; right: 0; height: 3px; }
.card.tone-green::before { background: #10b981; }
.card.tone-red::before { background: #d4232f; }
.card.tone-blue::before { background: #2563eb; }
.card.tone-amber::before { background: #f59e0b; }
.card.tone-violet::before { background: #8b5cf6; }
.card.tone-slate::before { background: #475569; }
.card-label { font-size: 12px; font-weight: 600; color: #4b5b70; }
.card-value { font-size: 28px; font-weight: 800; color: #16202f; margin: 8px 0 6px; letter-spacing: -0.5px; line-height: 1.1; }
.card.tone-green .card-value { color: #047857; }
.card.tone-red .card-value { color: #b31422; }
.card.tone-blue .card-value { color: #1d4ed8; }
.card-sub { font-size: 11px; color: #5b6879; line-height: 1.5; }

.chart-row { display: grid; grid-template-columns: 2fr 1fr; gap: 12px; }
.panel {
  background: #fff; border: 1px solid #dfe4ec; border-radius: 10px; padding: 14px 16px;
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05);
}
.panel-title {
  font-size: 13px; font-weight: 700; color: #223047; margin-bottom: 8px;
  display: flex; justify-content: space-between; align-items: center;
}
.panel-note { font-size: 11px; font-weight: 400; color: #7b8a9e; }
.chart { width: 100%; }
.h300 { height: 300px; }
.empty-hint { text-align: center; font-size: 12px; color: #7b8a9e; padding: 8px 0 0; }
.empty-block { padding: 36px; text-align: center; font-size: 13px; color: #7b8a9e; }

.table-wrap { max-height: 320px; overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid #eef1f6; color: #33445a; }
th { background: #f7f9fc; color: #4b5b70; font-weight: 700; position: sticky; top: 0; }
td.date { font-weight: 600; }
.bar-col { width: 36%; }
.bar { background: #e9edf3; border-radius: 4px; height: 9px; overflow: hidden; }
.fill { background: #10b981; height: 9px; border-radius: 4px; min-width: 2px; }

.notes {
  background: #f8fafc; border: 1px solid #e3e8f0; border-left: 3px solid #94a3b8;
  border-radius: 8px; padding: 12px 16px; display: flex; flex-direction: column; gap: 4px;
  font-size: 11px; color: #5b6879; line-height: 1.6;
}
.notes b { color: #33445a; font-size: 12px; }

@media (max-width: 1100px) {
  .chart-row { grid-template-columns: 1fr; }
}
</style>
