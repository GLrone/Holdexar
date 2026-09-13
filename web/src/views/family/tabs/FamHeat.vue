<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import { useI18n, useLocaleFormat } from '@/locales'
import { memberColor } from '@/lib/familyColors'
import { useFamilyStore } from '@/stores/familyLib'

/**
 * 入库热力图（GitHub 风格二维网格）：
 * - 数据源 = timeAcquired（rt_time_acquired 入库时间），**日粒度** dayMap；
 * - 一列 = 一周（周一 → 周日），7 行 = 星期；跨度自适应：首条入库日 → 今天；
 * - 视图约束：「全部成员」= 家庭整体聚合一张图，选中成员 = 只看该成员一张图
 *   （不再同屏堆叠所有人的分块图）；
 * - 密度分档 0-4（相对峰值色阶）；默认滚动到最右（最新数据）。
 */
const store = useFamilyStore()
const { t } = useI18n()
const fmt = useLocaleFormat()
onMounted(() => { if (!store.ready) void store.load() })

const wrap = ref<HTMLElement | null>(null)

interface HeatCell { key: string | null; lv: number }
interface HeatGrid {
  weeks: HeatCell[][]
  labels: { col: number; text: string }[]
  max: number
  total: number
  firstKey: string
  lastKey: string
}

function localKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** dayMap → 二维网格（列 = 周对齐周一，行 = 周一..周日；首条入库前留空不渲染） */
function buildGrid(dayMap: Record<string, number>): HeatGrid | null {
  const keys = Object.keys(dayMap).sort()
  if (!keys.length) return null
  const total = Object.values(dayMap).reduce((s, v) => s + v, 0)
  const max = Math.max(...Object.values(dayMap), 1)
  const firstKey = keys[0]!
  const lastKey = keys[keys.length - 1]!
  const first = new Date(`${firstKey}T00:00:00`)
  const start = new Date(first)
  start.setDate(first.getDate() - ((first.getDay() + 6) % 7))
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const weeks: HeatCell[][] = []
  const labels: { col: number; text: string }[] = []
  let lastMonth = -1
  const cursor = new Date(start)
  let col = 0
  while (cursor <= today) {
    const monday = new Date(cursor)
    const mm = monday.getMonth()
    const week: HeatCell[] = []
    for (let d = 0; d < 7; d++) {
      const k = localKey(cursor)
      const inRange = k >= firstKey && k <= lastKey
      const n = dayMap[k] ?? 0
      week.push({
        key: inRange ? k : null,
        lv: n === 0 ? 0 : Math.min(4, Math.ceil((n / max) * 4)),
      })
      cursor.setDate(cursor.getDate() + 1)
    }
    if (mm !== lastMonth) {
      // 轴标签走 useLocaleFormat()（zh `2024年1月`/`1月`，en `Jan 2024`/`Jan`，U3）。
      // 一月带上年份的分支是原有逻辑，保留。
      labels.push({ col, text: mm === 0 ? fmt.yearMonth(monday) : fmt.month(monday) })
      lastMonth = mm
    }
    weeks.push(week)
    col++
  }
  return { weeks, labels, max, total, firstKey, lastKey }
}

interface NamedGrid {
  sid: string
  name: string
  avatar: string
  color: string
  grid: HeatGrid | null
}

/** 成员图（渲染顺序 = 成员表序） */
const memberBlocks = computed<NamedGrid[]>(() =>
  store.members.map((m, i) => ({
    sid: m.steamid,
    name: m.personaName || t('famHeat.memberFallback', { id: m.steamid.slice(-4) }),
    avatar: m.avatarUrl || '',
    color: memberColor(i),
    grid: buildGrid(store.acquiredDayMaps.members[m.steamid] ?? {}),
  })),
)

/** 成员筛选（all = 家庭整体视图，只渲染聚合一张图） */
const activeMember = ref('all')
const memberChips = computed(() => [
  { sid: 'all', name: t('famHeat.filter.all'), avatar: '' },
  ...memberBlocks.value.map((b) => ({ sid: b.sid, name: b.name, avatar: b.avatar })),
])

const current = computed<NamedGrid | null>(() => {
  if (activeMember.value === 'all') {
    return {
      sid: 'all',
      name: t('famHeat.title.family'),
      avatar: '',
      color: 'var(--accent)',
      grid: buildGrid(store.acquiredDayMaps.family),
    }
  }
  return memberBlocks.value.find((b) => b.sid === activeMember.value) ?? null
})

function fmtKey(k: string): string {
  return `${k.slice(0, 4)}/${k.slice(5, 7)}/${k.slice(8, 10)}`
}
function countOf(dayMap: Record<string, number>, key: string): number {
  return dayMap[key] ?? 0
}

/* 默认滚到最右（最新数据）：数据就绪 / 切换视图后都要重新对齐 */
function scrollRight() {
  for (const el of wrap.value?.querySelectorAll<HTMLElement>('.hm-scroll') ?? []) {
    el.scrollLeft = el.scrollWidth
  }
}

onMounted(() => { void nextTick(scrollRight) })
watch(() => store.ready, (r) => { if (r) void nextTick(scrollRight) })
watch(activeMember, () => { void nextTick(scrollRight) })
</script>

<template>
  <div ref="wrap">
    <div v-if="store.error" class="lib-empty">{{ store.error }}</div>
    <div v-else-if="store.loading && !store.ready" class="lib-empty">{{ t('famHeat.empty.loading') }}</div>
    <div v-else-if="store.acquiredGames.length === 0" class="lib-empty">
      {{ t('famHeat.empty.noData') }}
    </div>

    <div v-else class="hm-wrap">
      <div class="hm-member-bar">
        <button
          v-for="c in memberChips"
          :key="c.sid"
          type="button"
          class="hm-member-btn"
          :class="{ 'is-on': activeMember === c.sid }"
          @click="activeMember = c.sid"
        >
          <template v-if="c.sid === 'all'">🔥</template>
          <HlImg
            v-else-if="c.avatar"
            class="hm-member-btn__ava"
            :src="c.avatar"
            :alt="c.name"
            loading="lazy"
          >
            <template #fallback>
              <span class="hm-member-btn__ava hm-member-btn__ava--fb">{{ c.name.slice(0, 1) }}</span>
            </template>
          </HlImg>
          <span v-else class="hm-member-btn__ava hm-member-btn__ava--fb">{{ c.name.slice(0, 1) }}</span>
          {{ c.name }}
        </button>
        <span v-if="store.fromSnapshot" class="hm-snap-tag">{{ t('famHeat.tag.snapshot') }}</span>
      </div>

      <!-- 单图约束：同一时刻只渲染一张热力图（聚合 或 选中成员） -->
      <div v-if="current" class="hm-block">
        <div class="hm-block__title">
          <span class="hm-block__dot" :style="{ background: current.color }"></span>
          {{ current.name }}
          <!-- 整句一条词条、行内 <b> 写在值里，v-html 渲染（同 bills 的先例）。
               日期区间与 `→` 留在组件侧：`→` 是纯符号（brief4 §5），两个日期由
               fmtKey 就地截成 YYYY/MM/DD，语言中立，不做词条。 -->
          <span v-if="current.grid" class="hm-block__meta">
            <span v-html="t('famHeat.meta', { total: current.grid.total, peak: current.grid.max })"></span>
            · {{ fmtKey(current.grid.firstKey) }} → {{ fmtKey(current.grid.lastKey) }}
          </span>
        </div>
        <div v-if="current.grid" class="hm-scroll">
          <div class="hm-months">
            <span
              v-for="l in current.grid.labels"
              :key="`m${l.col}`"
              :style="{ gridColumn: l.col + 1 }"
            >{{ l.text }}</span>
          </div>
          <div class="hm-grid">
            <i
              v-for="(c, ci) in current.grid.weeks.flat()"
              :key="`${ci}-${c.key ?? 'pad'}`"
              :class="{ 'is-empty': c.key === null }"
              :style="c.key ? { background: `var(--gh-${c.lv})` } : undefined"
              :title="c.key ? t('famHeat.tip.day', { date: fmtKey(c.key), count: countOf(activeMember === 'all' ? store.acquiredDayMaps.family : store.acquiredDayMaps.members[activeMember] ?? {}, c.key) }) : ''"
            />
          </div>
        </div>
        <div v-else class="hm-block__empty">{{ t('famHeat.empty.member') }}</div>
      </div>

      <div class="gh-legend">
        {{ t('famHeat.legend.low') }} <i style="background: var(--gh-0)"></i><i style="background: var(--gh-1)"></i><i style="background: var(--gh-2)"></i><i style="background: var(--gh-3)"></i><i style="background: var(--gh-4)"></i> {{ t('famHeat.legend.high') }}
      </div>
      <div class="hm-note">
        {{ t('famHeat.note') }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.lib-empty { text-align: center; padding: 26px 16px; border: 1px dashed var(--border-soft); border-radius: var(--radius); background: var(--surface-inset); font-size: 12.5px; color: var(--text-secondary); }

.hm-member-bar { display: flex; gap: 5px; margin-bottom: 10px; flex-wrap: wrap; align-items: center; }
.hm-member-btn {
  font-size: 11px; padding: 4px 11px; border-radius: 999px;
  border: 1px solid var(--border-soft); background: var(--surface-chip);
  color: var(--text-secondary); cursor: pointer; transition: var(--transition);
  display: flex; align-items: center; gap: 5px; font-family: inherit;
}
.hm-member-btn:hover { border-color: var(--accent); color: var(--accent); }
.hm-member-btn.is-on { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.hm-member-btn__ava { width: 15px; height: 15px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
.hm-member-btn__ava--fb { display: grid; place-items: center; font-size: 9px; font-weight: 600; background: var(--surface-chip-2); color: var(--text-dim); }
.hm-snap-tag { font-size: 10px; padding: 2px 8px; border-radius: 3px; background: var(--warn-a15, rgba(245, 158, 11, .15)); color: var(--warn, #f59e0b); font-weight: 600; }

.hm-block { background: var(--surface-inset); border: 1px solid var(--border-soft); border-radius: var(--radius); padding: 10px 12px; margin-bottom: 10px; }
.hm-block__title { display: flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 600; color: var(--text-primary); margin-bottom: 8px; }
.hm-block__dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.hm-block__meta { font-size: 10.5px; color: var(--text-dim); font-weight: 400; margin-left: auto; }
/* :deep() —— 这行的 <b> 由 v-html 注入，拿不到 scoped 属性（同 settings/Index.vue
   的第 2–4 步）。样式本身逐字未动。 */
.hm-block__meta :deep(b) { color: var(--text-secondary); font-family: var(--font-mono, monospace); }
.hm-block__empty { font-size: 11px; color: var(--text-dim); padding: 8px 0 4px; }

.hm-scroll { overflow-x: auto; padding-bottom: 4px; }
.hm-months { display: grid; grid-auto-columns: 11px; gap: 3px; margin-bottom: 5px; font-size: 9.5px; color: var(--text-dim); font-family: var(--font-mono, monospace); }
.hm-months span { grid-row: 1; white-space: nowrap; overflow: visible; }
.hm-grid { display: grid; grid-template-rows: repeat(7, 11px); grid-auto-flow: column; grid-auto-columns: 11px; gap: 3px; }
.hm-grid i { width: 11px; height: 11px; border-radius: 2px; transition: transform 0.12s var(--ease-out); }
.hm-grid i:not(.is-empty):hover { transform: scale(1.55); outline: 1.5px solid var(--accent); z-index: 2; position: relative; }
.hm-grid i.is-empty { background: transparent; pointer-events: none; }

.gh-legend { display: flex; align-items: center; gap: 4px; font-size: 10px; color: var(--text-dim); margin-top: 4px; justify-content: flex-end; font-family: var(--font-mono, monospace); }
.gh-legend i { width: 11px; height: 11px; border-radius: 2px; display: inline-block; }
.hm-note { margin-top: 8px; font-size: 10.5px; color: var(--text-dim); line-height: 1.7; }
</style>
