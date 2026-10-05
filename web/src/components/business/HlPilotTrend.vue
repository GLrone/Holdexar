<script setup lang="ts">
/**
 * 领航台价格走势迷你图：阶梯折线（价格在变化点之间保持不变）+ 现价点 + 最低点，
 * 悬停读数。纯 SVG，无图表库依赖；颜色只取设计 token。
 */
import { computed, ref } from 'vue'

import { formatCnyFen } from '@/api/regions'
import { useI18n } from '@/locales'

const props = defineProps<{
  /** [YYYY-MM-DD, 分]，时间升序 */
  points: [string, number][]
}>()

const { t } = useI18n()

interface Pt { t: number; v: number; d: string }

const series = computed<Pt[]>(() =>
  props.points
    .map(([d, v]) => ({ t: Date.parse(`${d}T00:00:00`), v, d }))
    .filter((p) => Number.isFinite(p.t) && p.v > 0),
)

const t0 = computed(() => series.value[0]?.t ?? 0)
const t1 = computed(() => Math.max(Date.now(), series.value[series.value.length - 1]?.t ?? 0))
const span = computed(() => Math.max(1, t1.value - t0.value))

const bounds = computed(() => {
  const vs = series.value.map((p) => p.v)
  const min = Math.min(...vs)
  const max = Math.max(...vs)
  const pad = (max - min) * 0.14 || max * 0.1
  return { min, max, lo: min - pad, hi: max + pad }
})

function xPct(ts: number): number {
  return ((ts - t0.value) / span.value) * 100
}
function yPct(v: number): number {
  const { lo, hi } = bounds.value
  return 100 - ((v - lo) / (hi - lo)) * 100
}

const linePath = computed(() => {
  const pts = series.value
  let d = ''
  pts.forEach((p, i) => {
    d += i === 0 ? `M${xPct(p.t)} ${yPct(p.v)}` : `H${xPct(p.t)}V${yPct(p.v)}`
  })
  return `${d}H100`
})
const areaPath = computed(() => `${linePath.value}V100H0Z`)

const nowPt = computed(() => series.value[series.value.length - 1])
const lowPt = computed(() => series.value.reduce((a, b) => (b.v < a.v ? b : a), series.value[0]))
const showLow = computed(() => lowPt.value && nowPt.value && lowPt.value.v < nowPt.value.v)

const hoverIdx = ref<number | null>(null)
const hoverPt = computed(() => (hoverIdx.value === null ? null : series.value[hoverIdx.value]))

function onMove(e: PointerEvent) {
  const el = e.currentTarget as HTMLElement
  const rect = el.getBoundingClientRect()
  if (rect.width <= 0) return
  const ts = t0.value + Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)) * span.value
  let idx = 0
  for (let i = 0; i < series.value.length; i++) {
    if (series.value[i].t <= ts) idx = i
    else break
  }
  hoverIdx.value = idx
}

const ariaLabel = computed(() =>
  t('pilot.card.trendAria', {
    min: formatCnyFen(bounds.value.min),
    max: formatCnyFen(bounds.value.max),
  }),
)
</script>

<template>
  <div v-if="series.length >= 2" class="ptrend">
    <div class="ptrend__head">
      <span class="ptrend__label">{{ t('pilot.card.trend') }}</span>
      <span v-if="hoverPt" class="ptrend__read">
        {{ t('pilot.card.trendSince', { date: hoverPt.d }) }}
        <b>{{ formatCnyFen(hoverPt.v) }}</b>
      </span>
      <span v-else class="ptrend__read">
        {{ t('pilot.card.trendRange', { min: formatCnyFen(bounds.min), max: formatCnyFen(bounds.max) }) }}
      </span>
    </div>
    <div
      class="ptrend__plot"
      role="img"
      :aria-label="ariaLabel"
      @pointermove="onMove"
      @pointerleave="hoverIdx = null"
    >
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        <path :d="areaPath" class="ptrend__area" />
        <path :d="linePath" class="ptrend__line" vector-effect="non-scaling-stroke" />
      </svg>
      <i v-if="hoverPt" class="ptrend__guide" :style="{ left: `${xPct(hoverPt.t)}%` }"></i>
      <i
        v-if="showLow && lowPt"
        class="ptrend__dot ptrend__dot--low"
        :style="{ left: `${xPct(lowPt.t)}%`, top: `${yPct(lowPt.v)}%` }"
        :title="t('pilot.card.trendLowTip', { price: formatCnyFen(lowPt.v), date: lowPt.d })"
      ></i>
      <i
        v-if="nowPt"
        class="ptrend__dot"
        :style="{ left: '100%', top: `${yPct(nowPt.v)}%` }"
      ></i>
    </div>
    <div class="ptrend__axis">
      <span>{{ series[0].d }}</span>
      <span>{{ t('pilot.card.trendToday') }}</span>
    </div>
  </div>
</template>

<style scoped>
.ptrend {
  display: grid;
  gap: 4px;
}

.ptrend__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
  color: var(--text-secondary);
}

.ptrend__read b {
  margin-left: 4px;
  font-weight: 600;
  color: var(--text-primary);
}

.ptrend__plot {
  position: relative;
  height: 64px;
  margin: 2px 5px 0 0;
  touch-action: none;
}

.ptrend__plot svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
}

.ptrend__area {
  fill: var(--accent-a15);
  stroke: none;
}

.ptrend__line {
  fill: none;
  stroke: var(--accent);
  stroke-width: 1.6;
  stroke-linejoin: round;
}

.ptrend__guide {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--text-secondary);
  opacity: 0.45;
  pointer-events: none;
}

.ptrend__dot {
  position: absolute;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--bg-card);
  transform: translate(-50%, -50%);
}

.ptrend__dot--low {
  background: var(--success);
}

.ptrend__axis {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-secondary);
}
</style>
