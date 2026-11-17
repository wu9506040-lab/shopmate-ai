<script setup lang="ts">
/**
 * 商品详情页 — 编辑部式商品页
 * 上半部：款级图集 + 购买区；下半部：参数信息 / 尺码表 / 同店其他款。
 * 同款所有尺码共用一套实拍图，切换尺码不改变图片。
 */
import { ref, computed, onMounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { createOrder, getProduct, isAuthed, listProducts } from '../api';
import { specForModel } from '../data/catalogSpecs';
import type { Product } from '../types';

const route = useRoute();
const router = useRouter();
const sku = computed(() => route.params.sku as string);
const product = ref<Product | null>(null);
const loading = ref(true);
const error = ref('');

/** 款级图集（后端按 model 返回，同款各尺码一致） */
const activeImg = ref(0);
const gallery = computed<string[]>(() => {
  const g = product.value?.sku_gallery;
  if (g && g.length) return g;
  return product.value ? [product.value.cover_url] : [];
});
const currentImage = computed(() => gallery.value[activeImg.value] || product.value?.cover_url || '');
function onImageError(e: Event) {
  const img = e.target as HTMLImageElement;
  if (!img.dataset.fb && product.value) {
    img.dataset.fb = '1';
    img.src = product.value.cover_url;
  }
}
function onRelatedImgError(e: Event, p: Product) {
  const img = e.target as HTMLImageElement;
  if (!img.dataset.fb) {
    img.dataset.fb = '1';
    img.src = p.cover_url;
  }
}

const currentModel = computed(
  () => (product.value?.attributes as Record<string, unknown> | null)?.model as string | undefined,
);
const spec = computed(() => specForModel(currentModel.value));
const activeSize = computed(() =>
  String((product.value?.attributes as Record<string, unknown> | null)?.spec_label ?? ''),
);
const category = computed(() =>
  String((product.value?.attributes as Record<string, unknown> | null)?.category ?? '服饰'),
);

const lastModel = ref<string | null>(null);

async function load() {
  const firstLoad = !product.value;
  if (firstLoad) loading.value = true;
  error.value = '';
  try {
    const p = await getProduct(sku.value);
    const model = String((p.attributes as Record<string, unknown> | null)?.model ?? '');
    const modelChanged = model !== lastModel.value;
    if (modelChanged) activeImg.value = 0;
    product.value = p;
    lastModel.value = model;
    // 同款内切尺码：图片与推荐位都不重算，只换规格数据
    if (modelChanged) await Promise.all([loadRelated(), loadVariants()]);
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败';
  } finally {
    loading.value = false;
  }
}

// =============================================================
// 同店其他款（按款去重，跨品类优先）
// =============================================================
const related = ref<Product[]>([]);
const attrsOf = (p: Product) => (p.attributes ?? {}) as Record<string, unknown>;

async function loadRelated() {
  if (!product.value) return;
  try {
    const data = await listProducts({ limit: 50 });
    const seen = new Set<string>();
    const others: Product[] = [];
    for (const p of data.products) {
      const model = String(attrsOf(p).model ?? p.sku);
      if (model === currentModel.value || seen.has(model)) continue;
      seen.add(model);
      others.push(p);
    }
    const curCat = category.value;
    others.sort(
      (a, b) =>
        Number(String(attrsOf(a).category ?? '') === curCat) -
        Number(String(attrsOf(b).category ?? '') === curCat),
    );
    related.value = others.slice(0, 4);
  } catch {
    /* 静默 */
  }
}

// =============================================================
// 尺码规格
// =============================================================
const variants = ref<Product[]>([]);

async function loadVariants() {
  if (!currentModel.value) {
    variants.value = [];
    return;
  }
  try {
    const data = await listProducts({ limit: 50 });
    variants.value = data.products.filter(
      (p) => attrsOf(p).model === currentModel.value,
    );
  } catch {
    variants.value = [];
  }
}

function specLabel(p: Product): string {
  return String(attrsOf(p).spec_label ?? p.sku.split('-').pop() ?? '');
}

function switchVariant(s: string) {
  if (s === product.value?.sku) return;
  router.push({ name: 'product-detail', params: { sku: s } });
}

function onAsk() {
  if (!product.value) return;
  // 带上款号与商品名：AI 客服据此绑定该款切片，且横幅显示商品名而非裸 SKU
  router.push({
    name: 'chat',
    query: {
      sku: product.value.sku,
      model: currentModel.value || '',
      name: product.value.name,
    },
  });
}

const buying = ref(false);
const buyError = ref<string | null>(null);
const isLoggedIn = computed(() => isAuthed.value === true);

async function buyNow() {
  if (!product.value || buying.value) return;
  buyError.value = null;
  if (!isLoggedIn.value) {
    router.push({ name: 'login', query: { redirect: `/shop/${product.value.sku}` } });
    return;
  }
  buying.value = true;
  try {
    const r = await createOrder({ sku: product.value.sku, qty: 1 });
    router.push({ name: 'profile', query: { highlight: r.order_no } });
  } catch (e) {
    buyError.value = e instanceof Error ? e.message : '下单失败';
  } finally {
    buying.value = false;
  }
}

function openProduct(s: string) {
  router.push({ name: 'product-detail', params: { sku: s } });
}

onMounted(load);
watch(sku, load);
</script>

<template>
  <main class="detail-page">
    <div class="detail-inner">
      <nav class="breadcrumb">
        <a @click="router.push('/shop')">全部款式</a>
        <span>/</span>
        <span>{{ product?.name || '加载中…' }}</span>
      </nav>

      <div v-if="loading" class="state"><div class="spinner"></div><p>正在取货…</p></div>
      <div v-else-if="error" class="state error">{{ error }}</div>

      <template v-else-if="product">
        <div class="detail-grid">
          <!-- 左：款级实拍图集 -->
          <div class="left">
            <div class="cover">
              <img :src="currentImage" :alt="product.name" @error="onImageError" />
              <span v-if="gallery.length > 1" class="cover-idx">
                {{ String(activeImg + 1).padStart(2, '0') }} / {{ String(gallery.length).padStart(2, '0') }}
              </span>
            </div>
            <div class="thumbs">
              <button
                v-for="(src, i) in gallery"
                :key="src"
                class="thumb"
                :class="{ active: i === activeImg }"
                @click="activeImg = i"
              >
                <img :src="src" :alt="`${product.name} 图 ${i + 1}`" @error="onImageError" />
              </button>
            </div>
            <p class="gallery-note">同款实拍 · 尺码不同不换图</p>
          </div>

          <!-- 右：购买区 -->
          <div class="right">
            <span class="eyebrow">CAT &amp; MOUSE · 2026 AW / {{ category }}</span>
            <h1>{{ product.name }}</h1>

            <div class="price-line">
              <span class="price">¥{{ product.price.toLocaleString('zh-CN') }}</span>
              <span v-if="product.price_version" class="price-note">{{ product.price_label }}</span>
              <span class="stock" :class="{ out: product.stock === 0 }">
                {{ product.stock > 0 ? (product.stock < 5 ? `仅剩 ${product.stock} 件` : '现货') : '暂时无货' }}
              </span>
            </div>

            <p v-if="product.description" class="description">{{ product.description }}</p>

            <div v-if="spec?.highlights.length" class="quick-facts">
              <span v-for="h in spec.highlights" :key="h.label">
                <em>{{ h.label }}</em>{{ h.value }}
              </span>
            </div>

            <!-- 规格 -->
            <div v-if="variants.length > 1" class="block">
              <div class="block-title"><span>选择尺码</span><em>{{ variants.length }} 种规格</em></div>
              <div class="spec-options">
                <button
                  v-for="v in variants"
                  :key="v.sku"
                  class="spec-btn"
                  :class="{ active: v.sku === product.sku, out: v.stock === 0 }"
                  :disabled="v.stock === 0"
                  @click="switchVariant(v.sku)"
                >
                  {{ specLabel(v) }}
                </button>
              </div>
              <p class="spec-hint">下方提供本款完整尺码表与选码建议</p>
            </div>

            <!-- 服务承诺 -->
            <div class="block">
              <div class="block-title"><span>服务与保障</span></div>
              <ul class="service">
                <li>签收 7 天内吊牌完整、未洗涤可无理由退换，商家承担运费</li>
                <li>签收 15 天内出现开线 / 破损等质量问题，往返运费由商家承担</li>
                <li>现货 48 小时内发出，满 99 元包邮；已水洗或吊牌剪除不在无理由范围</li>
              </ul>
            </div>

            <div class="actions">
              <button
                class="btn-solid"
                :disabled="buying || product.stock === 0"
                @click="buyNow"
              >
                {{ buying ? '下单中…' : (product.stock === 0 ? '暂时无货' : '立即购买') }}
              </button>
              <button class="btn-line" @click="onAsk">咨询 / 尺码 ↗</button>
            </div>
            <p class="hint">登录后即可下单 · AI 客服 24h 在线</p>
            <div v-if="buyError" class="buy-error">{{ buyError }}</div>
          </div>
        </div>

        <!-- ============ 参数信息 ============ -->
        <section v-if="spec" class="spec-section">
          <div class="section-head">
            <span class="section-no">01 / SPECIFICATIONS</span>
            <h2>参数信息</h2>
            <p>依据商品吊牌与商品页标注整理，单位与口径以表头为准。</p>
          </div>

          <div v-if="spec.highlights.length" class="highlight-grid">
            <div v-for="h in spec.highlights" :key="h.label" class="highlight-cell">
              <strong>{{ h.value }}</strong>
              <span>{{ h.label }}</span>
            </div>
          </div>

          <div class="param-grid">
            <div v-for="row in spec.params" :key="row.label" class="param-cell">
              <span class="param-label">{{ row.label }}</span>
              <span class="param-value">{{ row.value }}</span>
            </div>
          </div>
        </section>

        <!-- ============ 尺码表 ============ -->
        <section v-if="spec?.sizeTable" class="size-section">
          <div class="section-head">
            <span class="section-no">02 / SIZE GUIDE</span>
            <h2>尺码表</h2>
            <p>{{ spec.sizeNote }}</p>
          </div>

          <div class="size-meta">
            <span>{{ spec.category }} · {{ spec.sizeRange }}</span>
            <span>单位 {{ spec.sizeTable.unit }} · 平铺手工测量</span>
          </div>

          <div class="table-wrap">
            <table class="size-grid">
              <thead>
                <tr>
                  <th v-for="(c, i) in spec.sizeTable.cols" :key="i">{{ c }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(row, ri) in spec.sizeTable.rows"
                  :key="ri"
                  :class="{ active: row.size === activeSize }"
                >
                  <td
                    v-for="(c, ci) in spec.sizeTable!.cols"
                    :key="ci"
                    :class="{ 'size-key': ci === 0 }"
                  >
                    {{ ci === 0 ? row.size : (row[c] ?? '—') }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="size-note">
            {{ spec.sizeTable.note || '表值为平铺手工测量，1-3CM 误差属正常范围。' }}
            <template v-if="activeSize">当前选中规格：<b>{{ activeSize }}</b></template>
          </p>

          <div v-if="spec.fitReport?.length" class="fit-block">
            <h3>试穿参考</h3>
            <div class="table-wrap">
              <table class="fit-grid">
                <thead>
                  <tr><th>身高 (cm)</th><th>体重 (kg)</th><th>试穿尺码</th><th>适配范围</th></tr>
                </thead>
                <tbody>
                  <tr v-for="(f, i) in spec.fitReport" :key="i">
                    <td>{{ f.height }}</td><td>{{ f.weight }}</td>
                    <td class="size-key">{{ f.size }}</td><td>{{ f.note }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <div class="measure">
            <h3>量体方法</h3>
            <ul>
              <li><b>胸围</b>：沿腋下最丰满处水平围量一周，软尺贴身不勒紧</li>
              <li><b>腰围</b>：肚脐上方 2 指处水平围量一周（裤装选码以此为准）</li>
              <li><b>衣长</b>：后领中点垂直量至下摆</li>
              <li><b>裤长</b>：腰头上沿垂直量至裤脚</li>
            </ul>
            <p>宽松版型按推荐表选码即可，喜修身可小一码；加绒款建议大一码。身高体重跨界时以体重维度优先。</p>
            <button class="ask-size" @click="onAsk">拿不准？问 AI 尺码客服 ↗</button>
          </div>
        </section>

        <!-- ============ 同店其他款 ============ -->
        <section v-if="related.length" class="related">
          <div class="section-head">
            <span class="section-no">03 / YOU MAY ALSO LIKE</span>
            <h2>看了又看</h2>
            <p>同店其他款式 · 不同品类不同版型</p>
          </div>
          <div class="related-grid">
            <article
              v-for="(p, i) in related"
              :key="p.sku"
              class="related-item"
              @click="openProduct(p.sku)"
            >
              <div class="related-cover">
                <img :src="p.cover_url" :alt="p.name" @error="onRelatedImgError($event, p)" />
                <span class="related-idx">{{ String(i + 1).padStart(2, '0') }}</span>
              </div>
              <div class="related-cat">{{ attrsOf(p).category || '服饰' }}</div>
              <div class="related-name">{{ p.name }}</div>
              <div class="related-price">¥{{ p.price.toLocaleString('zh-CN') }}</div>
            </article>
          </div>
        </section>
      </template>
    </div>
  </main>
</template>

<style scoped>
.detail-page {
  flex: 1;
  overflow-y: auto;
  background: var(--gray-50);
  color: var(--gray-800);
}
.detail-inner {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: var(--sp-8) var(--sp-10) var(--sp-12);
}

.breadcrumb {
  display: flex; gap: var(--sp-3);
  font-size: 11px; letter-spacing: .1em;
  color: var(--gray-500);
  padding-bottom: var(--sp-5);
  border-bottom: 1px solid var(--gray-300);
  margin-bottom: var(--sp-10);
}
.breadcrumb a { cursor: pointer; color: var(--gray-600); }
.breadcrumb a:hover { color: var(--jd-red); }

.state { padding: 120px 20px; text-align: center; color: var(--gray-500); font-size: 13px; letter-spacing: .06em; }
.state.error { color: var(--danger); }
.spinner {
  width: 24px; height: 24px; margin: 0 auto var(--sp-4);
  border: 1px solid var(--gray-300); border-top-color: var(--jd-red);
  border-radius: 50%; animation: spin .9s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ---------------- 主区 ---------------- */
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  gap: var(--sp-12);
  align-items: start;
}
.cover {
  position: relative; width: 100%;
  aspect-ratio: 4 / 5; overflow: hidden; background: var(--gray-100);
}
.cover img { width: 100%; height: 100%; object-fit: cover; filter: saturate(.92); }
.cover-idx {
  position: absolute; right: 14px; bottom: 14px;
  font-family: var(--font-display); font-size: 11px; letter-spacing: .12em;
  color: var(--gray-800); background: rgba(11,11,10,.66); padding: 4px 10px;
}
.thumbs { display: flex; gap: var(--sp-2); margin-top: var(--sp-3); }
.thumb {
  width: 68px; height: 84px; padding: 0;
  background: var(--gray-100); border: 1px solid transparent; overflow: hidden;
}
.thumb img { width: 100%; height: 100%; object-fit: cover; }
.thumb:hover { border-color: var(--gray-400); }
.thumb.active { border-color: var(--jd-red); }
.gallery-note { margin-top: var(--sp-3); font-size: 10px; letter-spacing: .14em; color: var(--gray-500); }

.eyebrow { display: block; font-size: 10px; letter-spacing: .22em; color: var(--jd-red); margin-bottom: var(--sp-4); }
.right h1 {
  font-family: var(--font-display);
  font-size: clamp(26px, 2.6vw, 36px);
  font-weight: 400; line-height: 1.38; letter-spacing: .04em;
  color: var(--gray-900); margin-bottom: var(--sp-6);
}
.price-line {
  display: flex; align-items: baseline; gap: var(--sp-4);
  padding-bottom: var(--sp-6); border-bottom: 1px solid var(--gray-300);
}
.price { font-family: var(--font-display); font-size: 34px; color: var(--jd-red); letter-spacing: .02em; }
.price-note, .stock { font-size: 11px; letter-spacing: .08em; color: var(--gray-500); }
.stock.out { color: var(--danger); }
.description {
  margin: var(--sp-6) 0; font-size: 13px; line-height: 2;
  letter-spacing: .03em; color: var(--gray-600);
}
.quick-facts { display: flex; flex-wrap: wrap; gap: var(--sp-2) var(--sp-6); margin-bottom: var(--sp-2); }
.quick-facts span { font-size: 12px; color: var(--gray-700); }
.quick-facts em { font-style: normal; color: var(--gray-500); margin-right: var(--sp-2); font-size: 11px; }

.block { margin-top: var(--sp-8); }
.block-title {
  display: flex; justify-content: space-between; align-items: baseline;
  font-size: 11px; letter-spacing: .16em; color: var(--gray-500);
  padding-bottom: var(--sp-3); border-bottom: 1px solid var(--gray-300);
  margin-bottom: var(--sp-4);
}
.block-title span { color: var(--gray-700); }
.block-title em { font-style: normal; font-size: 10px; }
.spec-options { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.spec-btn {
  min-width: 62px; height: 38px; padding: 0 14px;
  background: transparent; border: 1px solid var(--gray-300);
  color: var(--gray-600); font-size: 12px; letter-spacing: .06em;
}
.spec-btn:hover:not(:disabled) { border-color: var(--jd-red); color: var(--jd-red); }
.spec-btn.active { border-color: var(--jd-red); color: var(--jd-red); background: var(--jd-red-light); }
.spec-btn.out { opacity: .35; cursor: not-allowed; text-decoration: line-through; }
.spec-hint { margin-top: var(--sp-3); font-size: 11px; color: var(--gray-500); }

.service { display: flex; flex-direction: column; gap: var(--sp-3); }
.service li {
  position: relative; padding-left: var(--sp-4);
  font-size: 12px; line-height: 1.8; color: var(--gray-600);
}
.service li::before {
  content: ''; position: absolute; left: 0; top: 9px;
  width: 4px; height: 4px; background: var(--jd-red);
}
.actions { display: flex; gap: var(--sp-3); margin-top: var(--sp-10); }
.btn-solid, .btn-line {
  flex: 1; height: 48px; border-radius: 0;
  font-size: 13px; letter-spacing: .08em; border: 1px solid transparent;
}
.btn-solid { background: var(--jd-red); color: #fff; }
.btn-solid:hover:not(:disabled) { background: var(--jd-red-hover); }
.btn-solid:disabled { opacity: .4; cursor: not-allowed; }
.btn-line { background: transparent; color: var(--gray-800); border-color: var(--gray-400); }
.btn-line:hover { border-color: var(--jd-red); color: var(--jd-red); }
.hint { margin-top: var(--sp-3); font-size: 11px; color: var(--gray-500); letter-spacing: .04em; }
.buy-error { margin-top: var(--sp-3); font-size: 12px; color: var(--danger); }

/* ---------------- 整宽区块 ---------------- */
.spec-section, .size-section, .related {
  margin-top: var(--sp-12);
  padding-top: var(--sp-10);
  border-top: 1px solid var(--gray-300);
}
.section-head { margin-bottom: var(--sp-8); }
.section-no { font-size: 10px; letter-spacing: .24em; color: var(--jd-red); }
.section-head h2 {
  font-family: var(--font-display); font-size: 30px; font-weight: 400;
  letter-spacing: .05em; color: var(--gray-800); margin: var(--sp-3) 0 var(--sp-2);
}
.section-head p { font-size: 12px; line-height: 1.9; color: var(--gray-500); }

.highlight-grid {
  display: grid; grid-template-columns: repeat(3, 1fr);
  border: 1px solid var(--gray-300); margin-bottom: var(--sp-8);
}
.highlight-cell {
  padding: var(--sp-6) var(--sp-5);
  display: flex; flex-direction: column; gap: var(--sp-2);
  border-right: 1px solid var(--gray-300);
}
.highlight-cell:last-child { border-right: none; }
.highlight-cell strong {
  font-family: var(--font-display); font-size: 20px; font-weight: 400;
  line-height: 1.4; color: var(--gray-800);
}
.highlight-cell span { font-size: 11px; letter-spacing: .12em; color: var(--gray-500); }

.param-grid {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: var(--sp-12);
}
.param-cell {
  display: flex; gap: var(--sp-4);
  padding: var(--sp-3) 0; border-bottom: 1px solid var(--gray-300);
  font-size: 12px; line-height: 1.8;
}
.param-label { flex: 0 0 96px; color: var(--gray-500); }
.param-value { color: var(--gray-700); word-break: break-all; }

.size-meta {
  display: flex; justify-content: space-between; gap: var(--sp-4);
  font-size: 11px; letter-spacing: .1em; color: var(--gray-500);
  margin-bottom: var(--sp-4);
}
.table-wrap { overflow-x: auto; }
.size-grid, .fit-grid {
  width: 100%; border-collapse: collapse; min-width: 560px;
}
.size-grid th, .size-grid td, .fit-grid th, .fit-grid td {
  padding: 13px 16px; text-align: left;
  border-bottom: 1px solid var(--gray-300);
  font-size: 12px; letter-spacing: .04em;
}
.size-grid th, .fit-grid th {
  font-weight: 400; color: var(--gray-500);
  border-top: 1px solid var(--gray-400); border-bottom: 1px solid var(--gray-400);
  font-size: 11px; letter-spacing: .12em;
}
.size-grid td, .fit-grid td { color: var(--gray-700); }
.size-grid th:first-child, .size-grid .size-key { color: var(--gray-800); font-family: var(--font-display); font-size: 15px; }
.size-grid tr.active td { background: var(--jd-red-light); color: var(--gray-800); }
.size-grid tr.active .size-key { color: var(--jd-red); }
.size-note { margin-top: var(--sp-4); font-size: 11px; line-height: 1.9; color: var(--gray-500); }
.size-note b { color: var(--jd-red); font-weight: 400; margin-left: var(--sp-2); }

.fit-block, .measure { margin-top: var(--sp-10); }
.fit-block h3, .measure h3 {
  font-family: var(--font-display); font-size: 18px; font-weight: 400;
  letter-spacing: .06em; color: var(--gray-800); margin-bottom: var(--sp-4);
}
.fit-grid { min-width: 480px; }
.measure ul { display: grid; grid-template-columns: repeat(2, 1fr); gap: var(--sp-3) var(--sp-8); }
.measure li { font-size: 12px; line-height: 1.9; color: var(--gray-600); }
.measure li b { color: var(--gray-800); font-weight: 400; margin-right: var(--sp-2); }
.measure > p { margin-top: var(--sp-4); font-size: 12px; line-height: 1.9; color: var(--gray-500); }
.ask-size {
  margin-top: var(--sp-5); height: 40px; padding: 0 22px;
  background: transparent; border: 1px solid var(--gray-400);
  color: var(--gray-800); font-size: 12px; letter-spacing: .06em;
}
.ask-size:hover { border-color: var(--jd-red); color: var(--jd-red); }

/* ---------------- 同店其他款 ---------------- */
.related-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-6); }
.related-item { cursor: pointer; }
.related-cover { position: relative; aspect-ratio: 4 / 5; overflow: hidden; background: var(--gray-100); }
.related-cover img {
  width: 100%; height: 100%; object-fit: cover; filter: saturate(.9);
  transition: transform 520ms cubic-bezier(.2, 0, 0, 1), filter 300ms ease;
}
.related-item:hover .related-cover img { transform: scale(1.045); filter: saturate(1); }
.related-idx {
  position: absolute; top: 10px; left: 10px;
  font-family: var(--font-display); font-size: 11px; letter-spacing: .1em;
  color: var(--gray-800); background: rgba(11,11,10,.66); padding: 3px 8px;
}
.related-cat { margin-top: var(--sp-3); font-size: 10px; letter-spacing: .16em; color: var(--jd-red); }
.related-name {
  margin-top: var(--sp-2); font-size: 12px; line-height: 1.6; color: var(--gray-600);
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.related-price { margin-top: var(--sp-2); font-family: var(--font-display); font-size: 17px; color: var(--jd-red); }

@media (max-width: 900px) {
  .detail-inner { padding: var(--sp-6); }
  .detail-grid { grid-template-columns: 1fr; gap: var(--sp-8); }
  .related-grid { grid-template-columns: repeat(2, 1fr); gap: var(--sp-4); }
  .highlight-grid { grid-template-columns: 1fr; }
  .highlight-cell { border-right: none; border-bottom: 1px solid var(--gray-300); }
  .param-grid, .measure ul { grid-template-columns: 1fr; }
  .actions { flex-direction: column; }
}
</style>
