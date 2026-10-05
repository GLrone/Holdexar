<script setup lang="ts">
/**
 * 领航台游戏对比卡：2~3 款并排，按行比较现价 / 距史低 / 较近一年中位价 / 好评，
 * 每行最优项高亮。样式只取设计 token。
 */
import { computed } from 'vue'

import type { PilotCompareFacts } from '@/api/client'
import { formatCnyFen } from '@/api/regions'
import { useI18n } from '@/locales'
import { pilotCoverUrl, positivePct, ratingClass } from '@/lib/pilotView'
import HlImg from '@/components/ui/HlImg.vue'

const props = defineProps<{ card: PilotCompareFacts }>()
const { t, locale } = useI18n()

type Item = PilotCompareFacts['items'][number]

const countFmt = computed(() => new Intl.NumberFormat(locale.value, { notation: 'compact' }))

function fen(v: number | null | undefined): string {
  return typeof v === 'number' && v > 0 ? formatCnyFen(v) : '—'
}

function nameOf(g: Item): string {
  return g.name || `AppID ${g.appid}`
}

/** 距史低（分）：现价 − 史低，<=0 即当前就是史低 */
function gapLow(g: Item): number | null {
  if (typeof g.cnyFen !== 'number' || typeof g.lowestFen !== 'number') return null
  return Math.max(0, g.cnyFen - g.lowestFen)
}

/** 较近一年中位价的百分比偏差（负 = 便宜于常态） */
function vsMedian(g: Item): number | null {
  if (typeof g.cnyFen !== 'number' || !g.medianFen) return null
  return Math.round(((g.cnyFen - g.medianFen) / g.medianFen) * 100)
}

function bestIndex(values: (number | null)[], dir: 'min' | 'max'): number {
  let best = -1
  values.forEach((v, i) => {
    if (v === null) return
    if (best < 0) best = i
    else if (dir === 'min' ? v < (values[best] as number) : v > (values[best] as number)) best = i
  })
  // 全部相同没有"最优"可言
  const real = values.filter((v): v is number => v !== null)
  return real.length > 1 && real.every((v) => v === real[0]) ? -1 : best
}

const bestPrice = computed(() => bestIndex(props.card.items.map((g) => g.cnyFen ?? null), 'min'))
const bestLow = computed(() => bestIndex(props.card.items.map(gapLow), 'min'))
const bestMedian = computed(() => bestIndex(props.card.items.map(vsMedian), 'min'))
const bestRating = computed(() => bestIndex(props.card.items.map((g) => positivePct(g.positiveRate)), 'max'))

const gridStyle = computed(() => ({
  gridTemplateColumns: `52px repeat(${props.card.items.length}, minmax(88px, 1fr))`,
}))

function pctText(v: number | null): string {
  if (v === null) return '—'
  return `${v > 0 ? '+' : ''}${v}%`
}
</script>

<template>
  <div class="pcmp">
    <div class="pcmp__title">{{ t('pilot.compare.title') }}</div>
    <div class="pcmp__scroll">
      <div class="pcmp__grid" :style="gridStyle">
        <span></span>
        <router-link
          v-for="g in card.items"
          :key="`h${g.appid}`"
          class="pcmp__game"
          :to="`/game/${g.appid}`"
        >
          <HlImg :src="pilotCoverUrl(g.appid)" :alt="nameOf(g)" loading="lazy" class="pcmp__cover">
            <template #fallback>
              <span class="pcmp__cover-fallback">{{ nameOf(g).slice(0, 2) }}</span>
            </template>
          </HlImg>
          <span class="pcmp__name" :title="nameOf(g)">{{ nameOf(g) }}</span>
        </router-link>

        <span class="pcmp__k">{{ t('pilot.compare.price') }}</span>
        <span
          v-for="(g, i) in card.items"
          :key="`p${g.appid}`"
          class="pcmp__v pcmp__v--price"
          :class="{ 'is-best': i === bestPrice }"
        >
          {{ fen(g.cnyFen) }}
          <b v-if="g.discount" class="pcmp__disc">-{{ g.discount }}%</b>
          <small v-if="g.region" class="pcmp__region">{{ g.region }}</small>
        </span>

        <span class="pcmp__k">{{ t('pilot.compare.vsLow') }}</span>
        <span
          v-for="(g, i) in card.items"
          :key="`l${g.appid}`"
          class="pcmp__v"
          :class="{ 'is-best': i === bestLow }"
        >
          <template v-if="gapLow(g) === null">—</template>
          <template v-else-if="gapLow(g) === 0">{{ t('pilot.compare.atLow') }}</template>
          <template v-else>+{{ fen(gapLow(g)) }}</template>
        </span>

        <span class="pcmp__k">{{ t('pilot.compare.vsMedian') }}</span>
        <span
          v-for="(g, i) in card.items"
          :key="`m${g.appid}`"
          class="pcmp__v"
          :class="{ 'is-best': i === bestMedian }"
        >
          {{ pctText(vsMedian(g)) }}
        </span>

        <span class="pcmp__k">{{ t('pilot.compare.rating') }}</span>
        <span
          v-for="(g, i) in card.items"
          :key="`r${g.appid}`"
          class="pcmp__v pcmp__rating"
          :class="[ratingClass(g.positiveRate), { 'is-best': i === bestRating }]"
        >
          <template v-if="positivePct(g.positiveRate) === null">—</template>
          <template v-else>{{ positivePct(g.positiveRate) }}%</template>
          <small v-if="g.reviewCount">{{ countFmt.format(g.reviewCount) }}</small>
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pcmp {
  display: grid;
  gap: 8px;
  max-width: 100%;
  min-width: 0;
  overflow: hidden;
}

.pcmp__scroll {
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
  padding-bottom: 3px;
}

.pcmp__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.pcmp__grid {
  display: grid;
  align-items: center;
  gap: 8px 6px;
  font-size: 12px;
  min-width: fit-content;
}

.pcmp__game {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.pcmp__cover {
  width: 100%;
  aspect-ratio: 460 / 215;
  border-radius: 5px;
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
}

.pcmp__cover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.pcmp__cover-fallback {
  font-size: 11px;
  color: var(--text-secondary);
}

.pcmp__name {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.3;
  color: var(--text-primary);
}

.pcmp__game:hover .pcmp__name {
  color: var(--accent);
}

.pcmp__k {
  font-size: 11px;
  color: var(--text-secondary);
}

.pcmp__v {
  min-width: 0;
  padding: 3px 4px;
  border-radius: 6px;
  text-align: center;
  color: var(--text-primary);
}

.pcmp__v.is-best {
  background: var(--accent-a15);
  color: var(--accent);
  font-weight: 700;
}

.pcmp__v--price {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 3px;
  font-size: 14px;
  font-weight: 700;
}

.pcmp__disc {
  padding: 0 4px;
  border-radius: 4px;
  background: var(--success);
  color: var(--ink-on-fill);
  font-size: 10px;
  font-weight: 700;
}

.pcmp__region,
.pcmp__rating small {
  margin-left: 3px;
  font-size: 10px;
  font-weight: 400;
  color: var(--text-secondary);
}

.pcmp__rating {
  color: var(--success);
}

.pcmp__rating.medium {
  color: var(--warning);
}

.pcmp__rating.low {
  color: var(--text-secondary);
}

.pcmp__rating.is-best {
  color: var(--accent);
}
</style>
