<script setup lang="ts">
/**
 * 款卡片 — 编辑部式商品格
 * 一个商品卡 = 一个款；规格进详情页选择。
 * 图片占主视觉，卡片本身无边框、无底色，靠留白与细线建立秩序。
 */
import type { ModelGroup } from '../types';

const props = withDefaults(
  defineProps<{ group: ModelGroup; index?: number }>(),
  { index: 0 },
);
const emit = defineEmits<{ open: [sku: string]; ask: [sku: string] }>();

function priceText(g: ModelGroup): string {
  return g.minPrice === g.maxPrice
    ? `¥${g.minPrice.toLocaleString('zh-CN')}`
    : `¥${g.minPrice.toLocaleString('zh-CN')} — ${g.maxPrice.toLocaleString('zh-CN')}`;
}
</script>

<template>
  <article class="card" :style="{ '--i': props.index }" @click="emit('open', props.group.defaultSku)">
    <div class="frame">
      <img :src="props.group.cover" :alt="props.group.name" loading="lazy" />
      <span class="frame-no">{{ String(props.index + 1).padStart(2, '0') }}</span>
      <span class="frame-view">查看款式 ↗</span>
    </div>

    <div class="meta">
      <div class="meta-top">
        <span class="cat">{{ props.group.category }}</span>
        <span class="spec">{{ props.group.specCount }} 种规格</span>
      </div>
      <h3 class="name">{{ props.group.name }}</h3>
      <div class="meta-bottom">
        <span class="price">{{ priceText(props.group) }}</span>
        <button class="ask" @click.stop="emit('ask', props.group.defaultSku)">问客服 ↗</button>
      </div>
    </div>
  </article>
</template>

<style scoped>
.card {
  display: flex;
  flex-direction: column;
  cursor: pointer;
  animation: rise 480ms both;
  animation-delay: calc(var(--i, 0) * 40ms);
}
@keyframes rise {
  from { opacity: 0; transform: translateY(10px); }
  to   { opacity: 1; transform: translateY(0); }
}

.frame {
  position: relative;
  aspect-ratio: 4 / 5;
  overflow: hidden;
  background: var(--gray-100);
}
.frame img {
  width: 100%; height: 100%;
  object-fit: cover;
  filter: saturate(.9);
  transition: transform 520ms cubic-bezier(.2, 0, 0, 1), filter 300ms ease;
}
.card:hover .frame img { transform: scale(1.045); filter: saturate(1); }

.frame-no {
  position: absolute; top: 12px; left: 12px;
  font-family: var(--font-display);
  font-size: 12px; letter-spacing: .1em;
  color: var(--gray-800);
  background: rgba(11, 11, 10, .62);
  padding: 3px 8px;
}
.frame-view {
  position: absolute; left: 0; right: 0; bottom: 0;
  padding: 12px 14px;
  font-size: 11px; letter-spacing: .14em;
  color: #fff;
  background: linear-gradient(0deg, rgba(11,11,10,.82), rgba(11,11,10,0));
  opacity: 0;
  transform: translateY(6px);
  transition: opacity 260ms ease, transform 260ms ease;
}
.card:hover .frame-view { opacity: 1; transform: translateY(0); }

.meta { padding: var(--sp-4) 0 0; }
.meta-top {
  display: flex; justify-content: space-between;
  font-size: 10px; letter-spacing: .16em;
  color: var(--gray-500);
  margin-bottom: var(--sp-2);
}
.cat { color: var(--jd-red); }
.name {
  font-size: 14px; font-weight: 400; line-height: 1.6;
  color: var(--gray-800);
  letter-spacing: .02em;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 44px;
}
.meta-bottom {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: var(--sp-3);
  margin-top: var(--sp-3);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--gray-300);
}
.price {
  font-family: var(--font-display);
  font-size: 19px; color: var(--jd-red); letter-spacing: .02em;
}
.ask {
  background: none; border: none; padding: 0;
  font-size: 11px; letter-spacing: .08em;
  color: var(--gray-500);
  white-space: nowrap;
}
.ask:hover { color: var(--jd-red); }

@media (max-width: 900px) {
  .name { font-size: 13px; }
  .price { font-size: 16px; }
  .frame-view { display: none; }
}
</style>
