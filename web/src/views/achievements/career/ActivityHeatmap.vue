<script setup lang="ts">
/* 活跃热力图：解锁日历（年历）+ 星期×时段矩阵 + 时段分布 + 月度曲线 + 生涯统计。
 *
 * 口径必须先说清：这里的每一格是「当天**解锁的成就数**」，不是游玩时长——
 * Steam 公开页只给到「成就解锁时刻」，逐日/逐时游玩时长在公开数据里不存在
 * （社区侧写同款做法，见模式标题下的 note 词条）。把格子读成「玩了多久」
 * 是这类热力图最常见的误读，故标题下方常驻一句口径说明。
 *
 * 全部手绘 CSS/SVG：矩阵与柱条是纯几何，走 ECharts 只会引入整套 option 契约
 * （animation / axisPointer / tooltip 主题化）而收益为零。 */
import { computed, ref } from 'vue'

import type { CareerPayload } from '@/api/client'
import { HlIcon, HlPaneSwitch, HlSegmented, HlStat, type HlSelectOption } from '@/components/ui'
import { useI18n, useLocaleFormat } from '@/locales'
import type { MessageKey } from '@/locales'
import { PALETTE } from '@/lib/familyColors'
import { fmtDate } from './careerUtil'

const props = defineProps<{ career: CareerPayload }>()

const { t } = useI18n()
const { month: fmtMonth, yearMonth: fmtYearMonth } = useLocaleFormat()

/* ── 日期工具（一律本地时区手撕，不走 Date 解析字符串——'2025-08' 会被当 UTC）── */
function ymd(y: number, m: number, d: number): string {
  return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`
}

function parseYmd(s: string): Date {
  const [y, m, d] = s.split('-').map(Number)
  return new Date(y, (m ?? 1) - 1, d ?? 1)
}

function parseYm(s: string): Date {
  const [y, m] = s.split('-').map(Number)
  return new Date(y, (m ?? 1) - 1, 1)
}

/** 周一为一周之首（与后端 `date.weekday()` 同口径） */
function mondayIndex(d: Date): number {
  return (d.getDay() + 6) % 7
}

const act = computed(() => props.career.activity)

/* ── 年份筛选 ── */
const yearOptions = computed<HlSelectOption[]>(() => [
  { label: t('achievements.career.heat.yearAll'), value: 'all' },
  ...props.career.yearly.map((y) => ({ label: String(y.year), value: String(y.year) })),
])

const year = ref<string>('all')

/* ── 日历格子 ── */
interface HeatCell {
  key: string
  date: string
  count: number
  out: boolean
}

const weeks = computed<HeatCell[][]>(() => {
  const days = act.value.days
  const out: HeatCell[][] = []
  if (!act.value.firstDate || !act.value.lastDate) return out

  let start: Date
  let end: Date
  if (year.value === 'all') {
    start = parseYmd(act.value.firstDate)
    end = parseYmd(act.value.lastDate)
  } else {
    const y = Number(year.value)
    start = new Date(y, 0, 1)
    end = new Date(y, 11, 31)
  }
  // 对齐到周一开头
  start = new Date(start.getFullYear(), start.getMonth(), start.getDate() - mondayIndex(start))

  const first = parseYmd(act.value.firstDate)
  const last = parseYmd(act.value.lastDate)
  const cursor = new Date(start)
  let week: HeatCell[] = []
  while (cursor <= end) {
    const key = ymd(cursor.getFullYear(), cursor.getMonth() + 1, cursor.getDate())
    const outOfRange = cursor < first || cursor > last
    week.push({
      key,
      date: key,
      count: outOfRange ? 0 : (days[key] ?? 0),
      out: outOfRange,
    })
    if (week.length === 7) {
      out.push(week)
      week = []
    }
    cursor.setDate(cursor.getDate() + 1)
  }
  if (week.length) {
    while (week.length < 7) {
      const c = new Date(cursor)
      const key = ymd(c.getFullYear(), c.getMonth() + 1, c.getDate())
      week.push({ key, date: key, count: 0, out: true })
      cursor.setDate(cursor.getDate() + 1)
    }
    out.push(week)
  }
  return out
})

/** 月份标签：某周含当月 1~7 日即打标，且与上一个标签至少隔 3 列（避免糊成一片） */
const monthLabels = computed(() => {
  const out: { col: number; label: string }[] = []
  let lastCol = -9
  weeks.value.forEach((w, i) => {
    const cell = w.find((c) => !c.out)
    if (!cell) return
    const d = parseYmd(cell.date)
    if (d.getDate() <= 7 && i - lastCol >= 3) {
      out.push({ col: i, label: fmtMonth(d) })
      lastCol = i
    }
  })
  return out
})

const calCols = computed(() => weeks.value.length || 1)

/** 强度分档：0 / 1-2 / 3-7 / 8-15 / 16+ —— 与图例 5 格一一对应 */
const LEVEL_OPACITY = [0, 0.34, 0.55, 0.78, 1]

function levelOf(count: number): number {
  if (count <= 0) return 0
  if (count >= 16) return 4
  if (count >= 8) return 3
  if (count >= 3) return 2
  return 1
}

function cellStyle(count: number): Record<string, string> {
  const lvl = levelOf(count)
  if (lvl === 0) return { background: 'var(--surface-track)' }
  return { background: 'var(--accent-fill)', opacity: String(LEVEL_OPACITY[lvl]) }
}

const peakDate = computed(() => act.value.busiestDay?.date ?? '')

function dayTip(c: HeatCell): string {
  return c.out
    ? ''
    : t('achievements.career.heat.dayTip', { date: c.date, n: c.count })
}

/* ── 星期 × 时段矩阵 ── */
const DOW_SHORT: MessageKey[] = [
  'achievements.career.heat.weekdayShort.0',
  'achievements.career.heat.weekdayShort.1',
  'achievements.career.heat.weekdayShort.2',
  'achievements.career.heat.weekdayShort.3',
  'achievements.career.heat.weekdayShort.4',
  'achievements.career.heat.weekdayShort.5',
  'achievements.career.heat.weekdayShort.6',
]

const DOW_LONG: MessageKey[] = [
  'achievements.career.heat.weekdayLong.0',
  'achievements.career.heat.weekdayLong.1',
  'achievements.career.heat.weekdayLong.2',
  'achievements.career.heat.weekdayLong.3',
  'achievements.career.heat.weekdayLong.4',
  'achievements.career.heat.weekdayLong.5',
  'achievements.career.heat.weekdayLong.6',
]

const matrix = computed(() => {
  const src = act.value.hourWeekday
  const rows: number[][] = []
  for (let wd = 0; wd < 7; wd++) {
    const row: number[] = []
    for (let h = 0; h < 24; h++) row.push(src[wd * 24 + h] ?? 0)
    rows.push(row)
  }
  return rows
})

const matrixMax = computed(() => Math.max(1, ...matrix.value.flat()))

function matrixCellStyle(count: number): Record<string, string> {
  if (count <= 0) return { background: 'var(--surface-track)' }
  const ratio = count / matrixMax.value
  return {
    background: 'var(--accent-fill)',
    opacity: String(Math.round((0.22 + ratio * 0.78) * 100) / 100),
  }
}

function matrixTip(wd: number, h: number, count: number): string {
  return t('achievements.career.heat.matrixTip', {
    day: t(DOW_LONG[wd]),
    hour: `${h}:00`,
    n: count,
  })
}

/* ── 时段 / 星期柱条 ── */
const hourMax = computed(() => Math.max(1, ...act.value.hourHistogram))

function hourTip(h: number): string {
  return t('achievements.career.heat.hourTip', { hour: `${h}:00`, n: act.value.hourHistogram[h] ?? 0 })
}

/* ── 作息倾向 ── */
const phases = computed(() => {
  const a = act.value
  const total = Math.max(1, a.totalUnlocks)
  return [
    { key: 'night' as const, color: PALETTE.violet, value: a.nightUnlocks },
    { key: 'morning' as const, color: PALETTE.gold, value: a.morningUnlocks },
    { key: 'day' as const, color: PALETTE.sky, value: a.dayUnlocks },
    { key: 'evening' as const, color: PALETTE.pink, value: a.eveningUnlocks },
    { key: 'weekend' as const, color: PALETTE.teal, value: a.weekendUnlocks },
  ].map((p) => ({ ...p, pct: Math.round((p.value / total) * 100) }))
})

const PHASE_KEY: Record<string, MessageKey> = {
  night: 'achievements.career.heat.phase.night',
  morning: 'achievements.career.heat.phase.morning',
  day: 'achievements.career.heat.phase.day',
  evening: 'achievements.career.heat.phase.evening',
  weekend: 'achievements.career.heat.phase.weekend',
}

/* ── 月度曲线（后端只给有解锁的月份，空档在此补齐）── */
const monthly = computed(() => {
  const src = new Map(act.value.monthly.map((m) => [m.month, m.count]))
  if (src.size === 0) return []
  const keys = [...src.keys()].sort()
  const start = parseYm(keys[0])
  const end = parseYm(keys[keys.length - 1])
  const out: { month: string; label: string; count: number }[] = []
  const cursor = new Date(start)
  while (cursor <= end) {
    const key = `${cursor.getFullYear()}-${String(cursor.getMonth() + 1).padStart(2, '0')}`
    out.push({ month: key, label: fmtYearMonth(cursor), count: src.get(key) ?? 0 })
    cursor.setMonth(cursor.getMonth() + 1)
  }
  return out
})

const monthlyMax = computed(() => Math.max(1, ...monthly.value.map((m) => m.count)))

/* ── 生涯统计 ── */
const stat = computed(() => {
  const a = act.value
  return {
    activeDays: a.activeDays,
    totalUnlocks: a.totalUnlocks,
    current: t('achievements.career.heat.stat.days', { n: a.currentStreak }),
    longest: t('achievements.career.heat.stat.days', { n: a.longestStreak }),
    longestEnd: fmtDate(a.longestStreakEnd),
    maxGap: t('achievements.career.heat.stat.days', { n: a.maxGapDays }),
    busiest: a.busiestDay
      ? t('achievements.career.heat.stat.busiestValue', {
          date: a.busiestDay.date,
          n: a.busiestDay.count,
        })
      : t('achievements.career.heat.stat.none'),
    firstDate: a.firstDate || t('achievements.career.heat.stat.none'),
    span: t('achievements.career.heat.stat.days', { n: a.spanDays }),
  }
})

const hasDays = computed(() => weeks.value.length > 0)
</script>

<template>
  <section data-section="achievements.section.heatmap" class="cr-stack">
    <header class="cr-sec-head">
      <h3>
        <HlIcon name="calendar" />
        {{ t('achievements.section.heatmap') }}
      </h3>
    </header>

    <div class="cr-card">
      <div class="cr-head">
        <span class="cr-head-title">
          <HlIcon name="calendar" />
          {{ t('achievements.career.heat.calendarTitle') }}
        </span>
        <span class="cr-head-hint">
          {{ t('achievements.career.heat.hint', { n: stat.activeDays }) }}
        </span>
      </div>

      <div class="cr-heat-toolbar">
        <HlSegmented v-model="year" :options="yearOptions" />
        <span class="cr-note">{{ t('achievements.career.heat.note') }}</span>
      </div>

      <HlPaneSwitch :pane-key="year">
        <div class="cr-heat-cal-box">
          <template v-if="hasDays">
            <div class="cr-heat-dow">
              <span v-for="(k, i) in DOW_SHORT" :key="i">{{ t(k) }}</span>
            </div>
            <div class="cr-heat-scroll">
              <div
                class="cr-heat-months"
                :style="{ gridTemplateColumns: `repeat(${calCols}, 12px)` }"
              >
                <span
                  v-for="m in monthLabels"
                  :key="m.col"
                  class="cr-heat-month"
                  :style="{ gridColumn: `${m.col + 1} / span 3` }"
                >
                  {{ m.label }}
                </span>
              </div>
              <div
                class="cr-heat-cal"
                :style="{ gridTemplateColumns: `repeat(${calCols}, 12px)` }"
              >
                <template v-for="(w, wi) in weeks" :key="wi">
                  <span
                    v-for="c in w"
                    :key="c.key"
                    class="cr-heat-cell"
                    :class="{ 'is-out': c.out, 'is-peak': c.date === peakDate, 'cr-glow': c.date === peakDate }"
                    :style="cellStyle(c.count)"
                    :title="dayTip(c)"
                  />
                </template>
              </div>
            </div>
          </template>
          <p v-else class="cr-note cr-empty">{{ t('achievements.career.heat.empty') }}</p>
        </div>
      </HlPaneSwitch>

      <div v-if="hasDays" class="cr-heat-legend">
        <span class="cr-note">{{ t('achievements.career.heat.less') }}</span>
        <i class="cr-heat-swatch" :style="{ background: 'var(--surface-track)' }" />
        <i
          v-for="(o, i) in LEVEL_OPACITY.slice(1)"
          :key="i"
          class="cr-heat-swatch"
          :style="{ background: 'var(--accent-fill)', opacity: String(o) }"
        />
        <span class="cr-note">{{ t('achievements.career.heat.more') }}</span>
      </div>

      <!-- 生涯统计：与日历同卡（统计是日历的读数，不该另起一块） -->
      <div class="cr-kpi cr-heat-stats">
        <HlStat
          size="sm"
          :color="PALETTE.teal"
          :style="{ borderColor: PALETTE.teal + '30' }"
          :label="t('achievements.career.heat.stat.activeDays')"
          :value="String(stat.activeDays)"
        />
        <HlStat
          size="sm"
          :color="PALETTE.lime"
          :style="{ borderColor: PALETTE.lime + '30' }"
          :label="t('achievements.career.heat.stat.totalUnlocks')"
          :value="String(stat.totalUnlocks)"
        />
        <HlStat
          size="sm"
          :color="PALETTE.sky"
          :style="{ borderColor: PALETTE.sky + '30' }"
          :label="t('achievements.career.heat.stat.current')"
          :value="stat.current"
        />
        <HlStat
          size="sm"
          :color="PALETTE.blue"
          :style="{ borderColor: PALETTE.blue + '30' }"
          :label="t('achievements.career.heat.stat.longest')"
          :value="stat.longest"
          :sub="t('achievements.career.heat.stat.longestEnd', { date: stat.longestEnd })"
        />
        <HlStat
          size="sm"
          :color="PALETTE.slate"
          :style="{ borderColor: PALETTE.slate + '30' }"
          :label="t('achievements.career.heat.stat.maxGap')"
          :value="stat.maxGap"
        />
        <HlStat
          size="sm"
          :color="PALETTE.gold"
          :style="{ borderColor: PALETTE.gold + '30' }"
          :label="t('achievements.career.heat.stat.busiest')"
          :value="stat.busiest"
        />
        <HlStat
          size="sm"
          :color="PALETTE.amber"
          :style="{ borderColor: PALETTE.amber + '30' }"
          :label="t('achievements.career.heat.stat.first')"
          :value="stat.firstDate"
        />
        <HlStat
          size="sm"
          :color="PALETTE.violet"
          :style="{ borderColor: PALETTE.violet + '30' }"
          :label="t('achievements.career.heat.stat.span')"
          :value="stat.span"
        />
      </div>
    </div>

    <div class="cr-grid2">
      <!-- 星期 × 时段矩阵 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="dashboard" />
            {{ t('achievements.career.heat.matrixTitle') }}
          </span>
          <span class="cr-head-hint">{{ t('achievements.career.heat.matrixHint') }}</span>
        </div>
        <div class="cr-matrix-box">
          <div class="cr-matrix-dow">
            <span v-for="(k, i) in DOW_SHORT" :key="i">{{ t(k) }}</span>
          </div>
          <div class="cr-matrix-main">
            <div class="cr-matrix">
              <template v-for="(row, wd) in matrix" :key="wd">
                <span
                  v-for="(n, h) in row"
                  :key="`${wd}-${h}`"
                  class="cr-matrix-cell"
                  :style="matrixCellStyle(n)"
                  :title="matrixTip(wd, h, n)"
                />
              </template>
            </div>
            <div class="cr-matrix-hours">
              <span v-for="h in 24" :key="h" class="cr-matrix-hour">
                {{ (h - 1) % 3 === 0 ? h - 1 : '' }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- 作息倾向 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="sun" />
            {{ t('achievements.career.heat.phaseTitle') }}
          </span>
          <span class="cr-head-hint">{{ t('achievements.career.heat.hourHint') }}</span>
        </div>
        <div class="cr-phases">
          <div v-for="p in phases" :key="p.key" class="cr-rank">
            <div class="cr-rank-top">
              <span class="cr-rank-name">{{ t(PHASE_KEY[p.key]) }}</span>
              <span class="cr-rank-meta">
                <b class="hl-num">{{ p.value }}</b>
                <span class="hl-num">{{ t('achievements.career.taste.share', { pct: p.pct }) }}</span>
              </span>
            </div>
            <div class="cr-bar">
              <i :style="{ width: `${p.pct}%`, background: p.color }" />
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="cr-grid2">
      <!-- 时段分布 24 柱 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">{{ t('achievements.career.heat.hourTitle') }}</span>
          <span class="cr-head-hint">{{ t('achievements.career.heat.hourHint') }}</span>
        </div>
        <div class="cr-bars">
          <span
            v-for="(n, h) in act.hourHistogram"
            :key="h"
            class="cr-bar-col"
            :title="hourTip(h)"
          >
            <i
              class="cr-bar-fill"
              :style="{
                height: `${Math.round((n / hourMax) * 100)}%`,
                background: n > 0 ? 'var(--accent-fill)' : 'var(--surface-track)',
              }"
            />
            <em class="cr-bar-label">{{ h % 3 === 0 ? h : '' }}</em>
          </span>
        </div>
      </div>

      <!-- 月度曲线 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">{{ t('achievements.career.heat.monthTitle') }}</span>
          <span class="cr-head-hint">{{ t('achievements.career.heat.monthHint') }}</span>
        </div>
        <div class="cr-bars cr-bars--months">
          <span
            v-for="m in monthly"
            :key="m.month"
            class="cr-bar-col"
            :title="t('achievements.career.heat.monthTip', { month: m.label, n: m.count })"
          >
            <i
              class="cr-bar-fill"
              :style="{
                height: `${Math.round((m.count / monthlyMax) * 100)}%`,
                background: m.count > 0 ? 'var(--accent-fill)' : 'var(--surface-track)',
              }"
            />
          </span>
        </div>
        <div v-if="monthly.length" class="cr-month-axis">
          <span>{{ monthly[0].label }}</span>
          <span>{{ monthly[monthly.length - 1].label }}</span>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.cr-heat-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 8px;
}

/* ── 日历 ── */
/* 日历与矩阵都是「几百个小格」——不用 v-stagger 的逐格 blur 级联：
   上千个元素同时带 filter 是自找掉帧，整块淡入就够，逐格动效留给真正
   珍稀的那一格（is-peak 的呼吸微光）。
   左侧星期列 + 可横滚的周列（跨年视图 170+ 列必然超出容器）。 */
.cr-heat-cal-box {
  display: flex;
  gap: 6px;
  animation: crFadeUp calc(var(--duration-4) * var(--motion-scale)) var(--ease-out) backwards;
}

@keyframes crFadeUp {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
}

.cr-heat-dow {
  display: grid;
  grid-template-rows: repeat(7, 12px);
  gap: 3px;
  padding-top: 16px;
  flex: 0 0 auto;
}

.cr-heat-dow span {
  font-size: 9px;
  line-height: 12px;
  color: var(--text-dim);
}

.cr-heat-scroll {
  flex: 1 1 auto;
  min-width: 0;
  overflow-x: auto;
  overflow-y: hidden;
  padding-bottom: 2px;
}

.cr-heat-months {
  display: grid;
  height: 16px;
  gap: 3px;
}

.cr-heat-month {
  font-size: 9px;
  color: var(--text-dim);
  white-space: nowrap;
}

.cr-heat-cal {
  display: grid;
  grid-auto-flow: column;
  grid-template-rows: repeat(7, 12px);
  gap: 3px;
}

.cr-heat-cell {
  width: 12px;
  height: 12px;
  border-radius: 2px;
  background: var(--surface-track);
}

.cr-heat-cell.is-out {
  background: transparent;
}

.cr-heat-cell.is-peak {
  outline: 1px solid var(--gild-2);
  outline-offset: 1px;
}

.cr-heat-legend {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
}

.cr-heat-swatch {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  background: var(--surface-track);
}

.cr-heat-stats {
  margin-top: 10px;
}

/* 星期 × 时段矩阵 */
.cr-matrix-box {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  animation: crFadeUp calc(var(--duration-4) * var(--motion-scale)) var(--ease-out) backwards;
}

.cr-matrix-dow {
  display: grid;
  grid-template-rows: repeat(7, 11px);
  gap: 2px;
  flex: 0 0 auto;
}

.cr-matrix-dow span {
  font-size: 9px;
  line-height: 11px;
  color: var(--text-dim);
}

.cr-matrix-main {
  flex: 1 1 auto;
  min-width: 0;
  overflow-x: auto;
}

/* 矩阵：行 = 星期（7），列 = 小时（24）——DOM 序即行序，故用默认的 row 流，
   不能改成 column 流（那会把「同一天 24 小时」铺成一列）。 */
.cr-matrix {
  display: grid;
  grid-template-columns: repeat(24, 11px);
  grid-template-rows: repeat(7, 11px);
  gap: 2px;
}

.cr-matrix-cell {
  width: 11px;
  height: 11px;
  border-radius: 2px;
  background: var(--surface-track);
}

.cr-matrix-hours {
  display: grid;
  grid-template-columns: repeat(24, 11px);
  gap: 2px;
  margin-top: 3px;
}

.cr-matrix-hour {
  font-size: 8px;
  color: var(--text-dim);
  text-align: left;
}

/* 作息倾向 */
.cr-phases {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* 柱条（时段 / 月度共用）：底对齐的竖条 + 轴标签占位 */
.cr-bars {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 118px;
  padding-bottom: 14px;
  box-sizing: border-box;
}

.cr-bar-col {
  flex: 1 1 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  height: 100%;
  position: relative;
}

.cr-bar-fill {
  display: block;
  width: 100%;
  min-height: 2px;
  border-radius: 2px 2px 0 0;
  transition: height calc(var(--duration-5) * var(--motion-scale)) var(--ease-out);
}

.cr-bar-label {
  position: absolute;
  bottom: -13px;
  left: 0;
  font-size: 8px;
  font-style: normal;
  color: var(--text-dim);
}

.cr-bars--months {
  height: 98px;
}

.cr-month-axis {
  display: flex;
  justify-content: space-between;
  margin-top: 4px;
  font-size: 9px;
  color: var(--text-dim);
}
</style>