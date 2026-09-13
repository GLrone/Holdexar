<script setup lang="ts">
/* 增长趋势（）：
   月度入库累计折线——全家总计虚线 + 各成员彩色实线（SVG，无图表库依赖）；
   成员对比摘要：贡献最多 / 平均 / 最少；首末入库日期。 */
import { computed, onMounted } from 'vue'

import { GROWTH_COLORS } from '@/lib/familyColors'
import { useI18n } from '@/locales'
import { useFamilyStore } from '@/stores/familyLib'

const { t } = useI18n()
const store = useFamilyStore()
onMounted(() => { if (!store.ready) void store.load() })

const months = computed(() => store.monthlyAcquired.map((x) => x[0]))
const entries = computed(() => store.monthlyAcquired.map((x) => x[1]))

/** 累计序列：总计 + 各成员 */
const cum = computed(() => {
  const cumAll: number[] = []
  let total = 0
  const perMember: Record<string, number[]> = {}
  for (const m of store.members) perMember[m.steamid] = []
  const totals: Record<string, number> = {}
  for (const e of entries.value) {
    total += e.all
    cumAll.push(total)
    for (const m of store.members) {
      totals[m.steamid] = (totals[m.steamid] ?? 0) + (e.members[m.steamid] ?? 0)
      perMember[m.steamid]!.push(totals[m.steamid]!)
    }
  }
  return { cumAll, perMember }
})

const maxVal = computed(() => {
  const all = cum.value.cumAll[cum.value.cumAll.length - 1] ?? 1
  const memberMax = Math.max(
    ...store.members.map((m) => cum.value.perMember[m.steamid]?.at(-1) ?? 0),
    0,
  )
  return Math.max(all, memberMax, 1)
})

const SVG_W = 640
const SVG_H = 280
const P_L = 42
const P_R = 16
const P_T = 14
const P_B = 34
const cW = computed(() => SVG_W - P_L - P_R)
const cH = computed(() => SVG_H - P_T - P_B)
const step = computed(() => cW.value / (Math.max(months.value.length - 1, 1)))

function yOf(v: number): number {
  return P_T + cH.value - (v / maxVal.value) * cH.value
}
function xOf(i: number): number {
  return P_L + i * step.value
}

const gridLines = computed(() =>
  Array.from({ length: 6 }, (_, i) => {
    const v = Math.round((maxVal.value / 5) * i)
    return { v, y: yOf(v) }
  }),
)

const totalPoints = computed(() =>
  cum.value.cumAll.map((v, i) => `${xOf(i)},${yOf(v)}`).join(' '),
)

const memberLines = computed(() =>
  store.members.map((m, mi) => {
    const vals = cum.value.perMember[m.steamid] ?? []
    const pts = vals.map((v, i) => `${xOf(i)},${yOf(v)}`).join(' ')
    const dots = vals
      .map((v, i) => (v > 0 ? { cx: xOf(i), cy: yOf(v), r: 2 } : null))
      .filter((d): d is { cx: number; cy: number; r: number } => d !== null)
    return {
      steamid: m.steamid,
      name: m.personaName || t('famGrowth.member.fallback', { id: m.steamid.slice(-4) }),
      color: GROWTH_COLORS[mi % GROWTH_COLORS.length]!,
      pts,
      dots,
      last: vals.at(-1) ?? 0,
    }
  }),
)

/** x 轴年份标签（跨年时标一次） */
const yearLabels = computed(() => {
  const out: { x: number; year: string }[] = []
  let lastYear = ''
  months.value.forEach((m, i) => {
    const year = m.slice(0, 4)
    if (year !== lastYear) {
      out.push({ x: xOf(i), year })
      lastYear = year
    }
  })
  return out
})

/** 成员对比摘要 */
const compare = computed(() => {
  const totals = memberLines.value.map((m) => ({ name: m.name, count: m.last }))
    .sort((a, b) => b.count - a.count)
  const avg = totals.length
    ? Math.round(totals.reduce((s, x) => s + x.count, 0) / totals.length)
    : 0
  return {
    top: totals[0] ?? { name: '-', count: 0 },
    low: totals.at(-1) ?? { name: '-', count: 0 },
    avg,
  }
})

const span = computed(() => {
  const g = store.acquiredGames
  if (!g.length) return null
  const fmt = (ts: number) => {
    const d = new Date(ts * 1000)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  const sorted = [...g].sort((a, b) => a.timeAcquired - b.timeAcquired)
  return { first: fmt(sorted[0]!.timeAcquired), last: fmt(sorted.at(-1)!.timeAcquired), count: g.length }
})
</script>

<template>
  <div>
    <div v-if="store.error" class="lib-empty">{{ store.error }}</div>
    <div v-else-if="store.loading && !store.ready" class="lib-empty">{{ t('famGrowth.empty.loading') }}</div>
    <div v-else-if="store.acquiredGames.length === 0" class="lib-empty">
      {{ t('famGrowth.empty.noData') }}
    </div>

    <template v-else>
      <div class="gr-card">
        <div class="gr-card__title">{{ t('famGrowth.chart.title') }}</div>
        <div class="gr-scroll">
          <svg :viewBox="`0 0 ${SVG_W} ${SVG_H}`" class="gr-svg">
            <g v-for="gl in gridLines" :key="gl.v">
              <line :x1="P_L" :y1="gl.y" :x2="SVG_W - P_R" :y2="gl.y" class="gr-grid" />
              <text :x="P_L - 6" :y="gl.y + 4" text-anchor="end" class="gr-tick">{{ gl.v }}</text>
            </g>
            <line :x1="P_L" :y1="P_T + cH" :x2="SVG_W - P_R" :y2="P_T + cH" class="gr-axis" />
            <text v-for="yl in yearLabels" :key="yl.year" :x="yl.x" :y="P_T + cH + 18" text-anchor="middle" class="gr-tick">{{ yl.year }}</text>

            <!-- 总计虚线 -->
            <polyline :points="totalPoints" class="gr-total" />
            <!-- 成员彩线 + 数据点 -->
            <g v-for="m in memberLines" :key="m.steamid">
              <polyline :points="m.pts" class="gr-mline" :stroke="m.color" />
              <circle v-for="(d, i) in m.dots" :key="i" :cx="d.cx" :cy="d.cy" :r="d.r" :fill="m.color" opacity=".75" />
            </g>
          </svg>
        </div>
        <div class="gr-legend">
          <span class="gr-legend__item"><i class="gr-legend__dash"></i>{{ t('famGrowth.legend.total') }}</span>
          <span v-for="m in memberLines" :key="m.steamid" class="gr-legend__item">
            <i :style="{ background: m.color }"></i>{{ m.name }}
          </span>
        </div>
        <!-- 三行摘要各是一条整句词条，行内 <b>（含 class="gr-top" / "gr-low"）写在
             值里、由 v-html 渲染：注入节点拿不到 scoped 属性，故规则写成 :deep() -->
        <div class="gr-compare">
          <span v-html="t('famGrowth.compare.top', { name: compare.top.name, n: compare.top.count })"></span>
          <span v-html="t('famGrowth.compare.avg', { n: compare.avg })"></span>
          <span v-html="t('famGrowth.compare.low', { name: compare.low.name, n: compare.low.count })"></span>
        </div>
      </div>
      <div
        v-if="span"
        class="gr-span"
        v-html="t('famGrowth.span', { n: span.count, first: span.first, last: span.last })"
      ></div>
    </template>
  </div>
</template>

<style scoped>
.lib-empty { text-align: center; padding: 26px 16px; border: 1px dashed var(--border-soft); border-radius: var(--radius); background: var(--surface-inset); font-size: 12.5px; color: var(--text-secondary); }

.gr-card { background: var(--surface-inset); border: 1px solid var(--line-1); border-radius: var(--radius); padding: 10px 12px; }
.gr-card__title { font-size: 11.5px; font-weight: 600; color: var(--text-secondary); margin-bottom: 8px; }
.gr-scroll { overflow-x: auto; }
.gr-svg { width: 100%; min-width: 560px; display: block; }
.gr-grid { stroke: var(--line-2); stroke-width: 1; }
.gr-tick { fill: var(--text-dim); font-size: 10px; font-family: var(--font-mono, monospace); }
.gr-axis { stroke: var(--line-1); stroke-width: 1.2; }
.gr-total { fill: none; stroke: var(--text-dim); stroke-width: 1.5; stroke-dasharray: 6 3; }
.gr-mline { fill: none; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; opacity: .8; }

.gr-legend { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--line-2); }
.gr-legend__item { display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--text-secondary); }
.gr-legend__item i { width: 16px; height: 2.5px; border-radius: 2px; display: inline-block; }
.gr-legend__dash { border-top: 1.5px dashed var(--text-dim) !important; background: transparent !important; height: 0 !important; width: 16px !important; }

.gr-compare { display: flex; justify-content: center; gap: 16px; margin-top: 8px; font-size: 10.5px; color: var(--text-dim); }
.gr-compare :deep(b) { color: var(--text-secondary); }
.gr-compare :deep(.gr-top) { color: var(--success); }
.gr-compare :deep(.gr-low) { color: var(--warn, #f59e0b); }
.gr-span { display: flex; justify-content: center; gap: 6px; margin-top: 10px; font-size: 11px; color: var(--text-dim); }
.gr-span :deep(b) { color: var(--text-secondary); font-family: var(--font-mono, monospace); }
</style>
