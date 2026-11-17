<script setup lang="ts">
/**
 * 猫与小鼠 · 商城首页
 * 商品按 attributes.model 聚合成「款」，规格进详情页选择。
 */
import { ref, onMounted, computed } from 'vue';
import { useRouter, useRoute } from 'vue-router';
import { listProducts } from '../api';
import type { ModelGroup, Product } from '../types';
import ModelCard from '../components/ModelCard.vue';

const router = useRouter();
const route = useRoute();
const products = ref<Product[]>([]);
const loading = ref(true);
const error = ref('');
const activeCategory = ref<string | null>(null);

const categories = [
  { key: null, label: '全部' },
  { key: '外套', label: '外套' },
  { key: '裤装', label: '裤装' },
  { key: 'T恤', label: 'T恤' },
  { key: '毛衣', label: '毛衣' },
  { key: '衬衫', label: '衬衫' },
];

const promises = [
  '七天无理由 · 商家承担运费',
  '现货 48 小时内发货',
  '真实尺码表 · 身高体重选码',
  '签收 15 天质量问题包退换',
];

const searchText = computed(() => (route.query.q as string) || '');

function attrsOf(p: Product): Record<string, unknown> {
  return (p.attributes as Record<string, unknown>) || {};
}

function countFor(key: string | null): number {
  if (!key) return products.value.length;
  return products.value.filter((p) => String(attrsOf(p).category || '') === key).length;
}

const filtered = computed(() => {
  let list = products.value;
  if (activeCategory.value) {
    list = list.filter((p) => String(attrsOf(p).category || '') === activeCategory.value);
  }
  if (searchText.value) {
    const q = searchText.value.toLowerCase();
    list = list.filter(
      (p) =>
        p.name.toLowerCase().includes(q) ||
        p.sku.toLowerCase().includes(q) ||
        (p.description || '').toLowerCase().includes(q),
    );
  }
  return list;
});

/** 去掉 SKU 名尾部的规格串，得到款名 */
function modelName(p: Product): string {
  const n = p.name.trim();
  const label = String(attrsOf(p).spec_label || '');
  if (label && n.endsWith(label)) return n.slice(0, -label.length).trim();
  return n.replace(/\s+\d+(?:\.\d+)?(?:匹|L|kg|公斤).*$/, '').trim() || n;
}

/** 一个 model = 一张款卡 */
const groups = computed<ModelGroup[]>(() => {
  const map = new Map<string, Product[]>();
  for (const p of filtered.value) {
    const key = String(attrsOf(p).model || p.sku);
    const arr = map.get(key);
    if (arr) arr.push(p);
    else map.set(key, [p]);
  }
  return Array.from(map.entries()).map(([model, items]) => {
    const sorted = [...items].sort((a, b) => a.price - b.price);
    const first = sorted[0];
    const a = attrsOf(first);
    const prices = sorted.map((x) => x.price);
    return {
      model,
      category: String(a.category || '其它'),
      name: modelName(first),
      description: first.description || '',
      cover: first.cover_url,
      minPrice: Math.min(...prices),
      maxPrice: Math.max(...prices),
      specCount: sorted.length,
      defaultSku: first.sku,
      warrantyBody: typeof a.warranty_body === 'number' ? a.warranty_body : undefined,
      warrantyKeyPart: typeof a.warranty_compressor === 'number' ? a.warranty_compressor : undefined,
      priceVersion: first.price_version,
      priceValidTo: first.price_valid_to,
      specs: sorted.map((x) => String(attrsOf(x).spec_label || attrsOf(x).capacity || x.sku)),
    };
  });
});

const skuCount = computed(() => filtered.value.length);

onMounted(async () => {
  try {
    const data = await listProducts({ limit: 50 });
    products.value = data.products;
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载商品失败';
  } finally {
    loading.value = false;
  }
});

function onAsk(sku: string) {
  router.push({ name: 'chat', query: { sku } });
}
function openProduct(sku: string) {
  router.push({ name: 'product-detail', params: { sku } });
}
</script>

<template>
  <main class="shop">
    <!-- 编辑部式首屏：文字 / 竖构图形象图 / 数据 -->
    <section class="hero">
      <div class="hero-copy">
        <span class="eyebrow">CAT &amp; MOUSE — 2026 AUTUMN / WINTER</span>
        <h1>选得明白<br />穿得合身</h1>
        <p class="hero-desc">
          九款在售 · 上衣 / 裤装 / 毛衣三套尺码口径<br />
          现货 48 小时内发出，满 99 元包邮，七天无理由退换
        </p>
        <div class="hero-actions">
          <button class="btn-solid" @click="router.push('/chat')">咨询尺码客服</button>
          <button class="btn-line" @click="router.push('/shop')">浏览全部款 <span>↗</span></button>
        </div>
      </div>

      <figure class="hero-visual">
        <img src="/products/FS368998-scene.webp" alt="2026 秋冬 · 美式休闲短款夹克" />
        <figcaption>LOOK 01 — 美式休闲短款夹克</figcaption>
      </figure>

      <div class="hero-index">
        <div class="idx-row"><b>{{ String(groups.length).padStart(2, '0') }}</b><span>在售款</span></div>
        <div class="idx-row"><b>{{ String(products.length).padStart(2, '0') }}</b><span>可选规格</span></div>
        <div class="idx-row"><b>48</b><span>小时发货</span></div>
        <div class="idx-row"><b>07</b><span>天无理由</span></div>
      </div>
    </section>

    <!-- 承诺条：细线 + 序号，不用圆勾图标 -->
    <div class="promise-strip">
      <span v-for="(t, i) in promises" :key="t" class="promise">
        <i>0{{ i + 1 }}</i>{{ t }}
      </span>
    </div>

    <!-- 商品目录 -->
    <section class="catalog">
      <header class="catalog-head">
        <div>
          <span class="section-no">01 / COLLECTION</span>
          <h2>{{ searchText ? `搜索「${searchText}」` : (activeCategory || '全部款式') }}</h2>
        </div>
        <p class="head-note">
          {{ groups.length }} 款 · {{ skuCount }} 个规格　/　价格按当期价目展示，尺码与面料以详情页为准
        </p>
      </header>

      <nav class="cat-nav" aria-label="商品分类">
        <button
          v-for="cat in categories"
          :key="cat.key ?? 'all'"
          :class="{ active: activeCategory === cat.key }"
          @click="activeCategory = cat.key"
        >
          {{ cat.label }}<sup>{{ countFor(cat.key) }}</sup>
        </button>
      </nav>

      <div v-if="loading" class="state"><div class="spinner"></div><p>正在取货…</p></div>
      <div v-else-if="error" class="state error">{{ error }}</div>
      <div v-else-if="groups.length === 0" class="state">
        <p>没有匹配的款</p>
        <button class="btn-line" @click="activeCategory = null; router.push('/shop')">查看全部</button>
      </div>
      <div v-else class="grid">
        <ModelCard
          v-for="(g, i) in groups"
          :key="g.model"
          :group="g"
          :index="i"
          @open="openProduct"
          @ask="onAsk"
        />
      </div>
    </section>

    <footer class="shop-foot">
      <span>CAT &amp; MOUSE · 2026 AW</span>
      <span>尺码与面料成分以商品详情页为准</span>
    </footer>
  </main>
</template>

<style scoped>
.shop {
  flex: 1;
  overflow-y: auto;
  background: var(--gray-50);
  color: var(--gray-800);
}

/* ---------------- Hero ---------------- */
.hero {
  max-width: var(--content-max);
  margin: 0 auto;
  padding: var(--sp-12) var(--sp-10);
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px 150px;
  gap: var(--sp-12);
  align-items: center;
  border-bottom: 1px solid var(--gray-300);
}
.hero-copy { min-width: 0; }
.eyebrow {
  display: block;
  font-size: 10px;
  letter-spacing: .26em;
  color: var(--jd-red);
  margin-bottom: var(--sp-6);
}
.hero-copy h1 {
  font-family: var(--font-display);
  font-weight: 400;
  font-size: clamp(40px, 4.4vw, 64px);
  line-height: 1.14;
  letter-spacing: .05em;
  color: var(--gray-900);
}
.hero-desc {
  margin-top: var(--sp-6);
  font-size: 13px;
  line-height: 2;
  letter-spacing: .04em;
  color: var(--gray-600);
}
.hero-actions { display: flex; gap: var(--sp-3); margin-top: var(--sp-8); }
.btn-solid, .btn-line {
  height: 44px; padding: 0 24px;
  border-radius: 0;
  font-size: 13px; letter-spacing: .06em;
  border: 1px solid transparent;
}
.btn-solid { background: var(--jd-red); color: #fff; }
.btn-solid:hover { background: var(--jd-red-hover); }
.btn-line {
  background: transparent;
  color: var(--gray-800);
  border-color: var(--gray-400);
}
.btn-line:hover { border-color: var(--jd-red); color: var(--jd-red); }
.btn-line span { margin-left: 8px; }

.hero-visual { margin: 0; position: relative; }
.hero-visual img {
  width: 100%;
  aspect-ratio: 4 / 5;
  object-fit: cover;
  object-position: center 22%;
  filter: saturate(.86) contrast(1.03);
  background: var(--gray-100);
}
.hero-visual figcaption {
  margin-top: var(--sp-3);
  font-size: 10px; letter-spacing: .16em;
  color: var(--gray-500);
}

.hero-index {
  display: flex; flex-direction: column;
  gap: var(--sp-6);
  padding-left: var(--sp-6);
  border-left: 1px solid var(--gray-300);
}
.idx-row { display: flex; flex-direction: column; gap: 2px; }
.idx-row b {
  font-family: var(--font-display);
  font-size: 28px; font-weight: 400;
  color: var(--gray-900);
}
.idx-row span { font-size: 10px; letter-spacing: .14em; color: var(--gray-500); }

/* ---------------- 承诺条 ---------------- */
.promise-strip {
  max-width: var(--content-max); margin: 0 auto;
  padding: var(--sp-5) var(--sp-10);
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: var(--sp-6);
  border-bottom: 1px solid var(--gray-300);
}
.promise {
  display: flex; align-items: center; gap: var(--sp-3);
  font-size: 12px; letter-spacing: .04em; color: var(--gray-600);
}
.promise i {
  font-style: normal; font-family: var(--font-display);
  font-size: 14px; color: var(--jd-red);
}

/* ---------------- 目录 ---------------- */
.catalog {
  max-width: var(--content-max); margin: 0 auto;
  padding: var(--sp-12) var(--sp-10) var(--sp-12);
}
.catalog-head {
  display: flex; align-items: flex-end; justify-content: space-between;
  gap: var(--sp-8); margin-bottom: var(--sp-8);
}
.section-no {
  display: block; font-size: 10px; letter-spacing: .24em;
  color: var(--jd-red); margin-bottom: var(--sp-3);
}
.catalog-head h2 {
  font-family: var(--font-display);
  font-size: 34px; font-weight: 400; letter-spacing: .05em;
  color: var(--gray-800);
}
.head-note { font-size: 11px; letter-spacing: .04em; color: var(--gray-500); text-align: right; }

.cat-nav {
  display: flex; flex-wrap: wrap; gap: var(--sp-8);
  border-top: 1px solid var(--gray-300);
  border-bottom: 1px solid var(--gray-300);
  padding: var(--sp-4) 0;
  margin-bottom: var(--sp-10);
}
.cat-nav button {
  background: none; border: none; padding: 4px 0;
  font-size: 13px; letter-spacing: .08em; color: var(--gray-500);
  border-bottom: 1px solid transparent;
}
.cat-nav button:hover { color: var(--gray-800); }
.cat-nav button.active { color: var(--jd-red); border-bottom-color: var(--jd-red); }
.cat-nav sup { font-size: 10px; margin-left: 5px; color: var(--gray-500); }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(272px, 1fr));
  gap: var(--sp-10) var(--sp-6);
}

.state {
  padding: 96px 20px; text-align: center;
  color: var(--gray-500); font-size: 13px; letter-spacing: .06em;
}
.state.error { color: var(--danger); }
.state .btn-line { margin-top: var(--sp-6); display: inline-flex; align-items: center; }
.spinner {
  width: 24px; height: 24px; margin: 0 auto var(--sp-4);
  border: 1px solid var(--gray-300); border-top-color: var(--jd-red);
  border-radius: 50%; animation: spin .9s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.shop-foot {
  max-width: var(--content-max); margin: 0 auto;
  padding: var(--sp-6) var(--sp-10) var(--sp-12);
  display: flex; justify-content: space-between;
  border-top: 1px solid var(--gray-300);
  font-size: 10px; letter-spacing: .16em; color: var(--gray-500);
}

@media (max-width: 900px) {
  .hero { grid-template-columns: 1fr; padding: var(--sp-8) var(--sp-6); gap: var(--sp-8); }
  .hero-visual { max-width: 300px; }
  .hero-index { flex-direction: row; flex-wrap: wrap; border-left: none; padding-left: 0; border-top: 1px solid var(--gray-300); padding-top: var(--sp-4); gap: var(--sp-6); }
  .promise-strip { grid-template-columns: repeat(2, 1fr); padding: var(--sp-5) var(--sp-6); }
  .catalog { padding: var(--sp-8) var(--sp-6); }
  .catalog-head { flex-direction: column; align-items: flex-start; gap: var(--sp-3); }
  .head-note { text-align: left; }
  .grid { grid-template-columns: repeat(2, 1fr); gap: var(--sp-6) var(--sp-4); }
}
</style>
