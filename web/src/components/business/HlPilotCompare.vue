<script setup lang="ts">
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

function gapLow(g: Item): number | null {
  if (typeof g.cnyFen !== 'number' || typeof g.lowestFen !== 'number') return null
  return Math.max(0, g.cnyFen - g.lowestFen)
}

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
  const real = values.filter((v): v is number => v !== null)
  return real.length > 1 && real.every((v) => v === real[0]) ? -1 : best
}

const bestPrice = computed(() => bestIndex(props.card.items.map((g) => g.cnyFen ?? null), 'min'))
const bestLow = computed(() => bestIndex(props.card.items.map(gapLow), 'min'))
const bestMedian = computed(() => bestIndex(props.card.items.map(vsMedian), 'min'))
const bestRating = computed(() => bestIndex(props.card.items.map((g) => positivePct(g.positiveRate)), 'max'))

const gridStyle = computed(() => ({
  gridTemplateColumns: `72px repeat(${props.card.items.length}, minmax(100px, 1fr))`,
}))

function pctText(v: number | null): string {
  if (v === null) return '—'
  return `${v > 0 ? '+' : ''}${v}%`
}
</script>

<template>
  <div class="pcmp">
    <div class="pcmp__header">
      <svg class="pcmp__header-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
      </svg>
      <span class="pcmp__title">{{ t('pilot.compare.title') }}</span>
    </div>

    <div class="pcmp__scroll">
      <div class="pcmp__grid" :style="gridStyle">
        <span class="pcmp__corner"></span>
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

        <!-- 现价对比行 -->
        <span class="pcmp__k">{{ t('pilot.compare.price') }}</span>
        <div
          v-for="(g, i) in card.items"
          :key="`p${g.appid}`"
          class="pcmp__v pcmp__v--price"
          :class="{ 'is-best': i === bestPrice }"
        >
          <span class="pcmp__price-num">{{ fen(g.cnyFen) }}</span>
          <span v-if="g.discount" class="pcmp__disc">-{{ g.discount }}%</span>
          <span v-if="i === bestPrice" class="pcmp__best-badge">
            {{ t('pilot.regions.cheapest') }}
          </span>
        </div>

        <!-- 距史低行 -->
        <span class="pcmp__k">{{ t('pilot.compare.vsLow') }}</span>
        <div
          v-for="(g, i) in card.items"
          :key="`l${g.appid}`"
          class="pcmp__v"
          :class="{ 'is-best': i === bestLow }"
        >
          <template v-if="gapLow(g) === null">—</template>
          <span v-else-if="gapLow(g) === 0" class="pcmp__tag-at-low">{{ t('pilot.compare.atLow') }}</span>
          <template v-else>+{{ fen(gapLow(g)) }}</template>
        </div>

        <!-- 较一年中位价行 -->
        <span class="pcmp__k">{{ t('pilot.compare.vsMedian') }}</span>
        <div
          v-for="(g, i) in card.items"
          :key="`m${g.appid}`"
          class="pcmp__v"
          :class="{ 'is-best': i === bestMedian }"
        >
          <span :class="vsMedian(g) != null && vsMedian(g)! < 0 ? 'pcmp__diff-good' : 'pcmp__diff-high'">
            {{ pctText(vsMedian(g)) }}
          </span>
        </div>

        <!-- 好评率行 -->
        <span class="pcmp__k">{{ t('pilot.compare.rating') }}</span>
        <div
          v-for="(g, i) in card.items"
          :key="`r${g.appid}`"
          class="pcmp__v pcmp__rating"
          :class="[ratingClass(g.positiveRate), { 'is-best': i === bestRating }]"
        >
          <template v-if="positivePct(g.positiveRate) === null">—</template>
          <template v-else>
            <span class="pcmp__rate-val">{{ positivePct(g.positiveRate) }}%</span>
            <small v-if="g.reviewCount" class="pcmp__review-cnt">({{ countFmt.format(g.reviewCount) }})</small>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pcmp {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 100%;
  min-width: 0;
}

.pcmp__header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.pcmp__header-icon {
  width: 15px;
  height: 15px;
  color: var(--accent);
}

.pcmp__scroll {
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
  padding-bottom: 4px;
}

.pcmp__grid {
  display: grid;
  align-items: center;
  gap: 8px 10px;
  font-size: 12px;
  min-width: fit-content;
}

.pcmp__corner {
  display: block;
}

.pcmp__game {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  text-decoration: none;
  color: inherit;
  transition: transform var(--transition);
}

.pcmp__game:hover {
  transform: translateY(-2px);
}

.pcmp__cover {
  width: 100%;
  aspect-ratio: 460 / 215;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-inset);
  border: 1px solid var(--border-soft);
  display: grid;
  place-items: center;
  box-shadow: 0 2px 6px var(--surface-inset);
}

.pcmp__cover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.pcmp__cover-fallback {
  font-size: 11px;
  color: var(--text-muted);
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
  text-align: center;
}

.pcmp__game:hover .pcmp__name {
  color: var(--accent);
}

.pcmp__k {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-muted);
}

.pcmp__v {
  min-width: 0;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
  text-align: center;
  color: var(--text-primary);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  font-size: 12px;
}

.pcmp__v.is-best {
  background: var(--accent-a08);
  border-color: var(--accent-a30);
  font-weight: 600;
}

.pcmp__v--price {
  gap: 4px;
}

.pcmp__price-num {
  font-size: 14px;
  font-weight: 700;
  color: var(--text-primary);
  font-family: var(--font-mono);
}

.pcmp__disc {
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  background: var(--success);
  color: var(--ink-on-fill);
  font-size: 10px;
  font-weight: 700;
}

.pcmp__best-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--success);
  color: var(--ink-on-fill);
  font-weight: 600;
}

.pcmp__tag-at-low {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--success-a15);
  color: var(--success);
  font-weight: 600;
  font-size: 11px;
}

.pcmp__diff-good {
  color: var(--success);
  font-weight: 600;
}

.pcmp__diff-high {
  color: var(--text-secondary);
}

.pcmp__rating {
  font-size: 12px;
}

.pcmp__rate-val {
  font-weight: 600;
}

.pcmp__rating.good .pcmp__rate-val {
  color: var(--success);
}

.pcmp__rating.medium .pcmp__rate-val {
  color: var(--warning);
}

.pcmp__rating.low .pcmp__rate-val {
  color: var(--danger);
}

.pcmp__review-cnt {
  font-size: 10px;
  color: var(--text-faint);
}
</style>
