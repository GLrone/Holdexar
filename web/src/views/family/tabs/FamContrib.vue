<script setup lang="ts">
/* 贡献分布：
   - 堆叠条形图：X=成员、Y=入库数，堆叠档位 = N 人拥有（独占1人 → 5人共享分层），
     统计范围切换（累计总量 / 近半年）；
   - 成员占比环形图（donut）；
   - 近半年入库增量月度柱。 */
import { computed, onMounted, ref } from 'vue'

import { useI18n, useLocaleFormat } from '@/locales'
import { HlStat } from '@/components/ui'
import { memberColor, TIER_COLORS } from '@/lib/familyColors'
import { useFamilyStore } from '@/stores/familyLib'

const store = useFamilyStore()
const { t } = useI18n()
const fmt = useLocaleFormat()
onMounted(() => { if (!store.ready) void store.load() })

type RangeMode = 'all' | 'halfyear'
const rangeMode = ref<RangeMode>('all')

const halfYearCutoff = (() => {
  const d = new Date()
  d.setMonth(d.getMonth() - 6)
  return Math.floor(d.getTime() / 1000)
})()

/** 统计范围内的游戏（halfyear 仅近 6 个月入库） */
const scopedGames = computed(() =>
  store.games.filter((g) =>
    rangeMode.value === 'all' ? true : g.timeAcquired > halfYearCutoff,
  ),
)

/** 最大拥有档位（堆叠层数 = 1..maxOwners） */
const maxOwners = computed(() =>
  Math.min(6, Math.max(1, ...scopedGames.value.map((g) => g.ownerCount))),
)

interface Bar { sid: string; name: string; avatar: string; tiers: number[]; total: number }
/** 成员堆叠数据：tiers[n-1] = 该成员「n 人共享」档位的游戏数 */
const bars = computed<Bar[]>(() => {
  const rows = store.members.map((m, i) => {
    const tiers = Array<number>(maxOwners.value).fill(0)
    for (const g of scopedGames.value) {
      if (g.owners.includes(m.steamid)) {
        const tier = Math.min(g.ownerCount, maxOwners.value) - 1
        tiers[tier] = (tiers[tier] ?? 0) + 1
      }
    }
    return {
      sid: m.steamid,
      name: m.personaName || t('famContrib.memberFallback', { id: m.steamid.slice(-4) }),
      avatar: m.avatarUrl || '',
      color: memberColor(i),
      tiers,
      total: tiers.reduce((s, v) => s + v, 0),
    }
  })
  rows.sort((a, b) => b.total - a.total)
  return rows
})

const barMax = computed(() => Math.max(...bars.value.map((b) => b.total), 1))

/** 环形图（成员总贡献占比，对齐 renderContributionExtras donut） */
const donutTotal = computed(() => bars.value.reduce((s, b) => s + b.total, 0))
const donutSegments = computed(() => {
  const R = 52
  const C = 2 * Math.PI * R
  let acc = 0
  return bars.value.map((b, i) => {
    const frac = donutTotal.value ? b.total / donutTotal.value : 0
    const seg = {
      color: memberColor(i),
      dasharray: `${frac * C} ${C}`,
      dashoffset: -acc * C,
      pct: Math.round(frac * 100),
      name: b.name,
      avatar: b.avatar,
      count: b.total,
    }
    acc += frac
    return seg
  })
})

/** 近半年月度增量（对齐 halfyear-chart：最近 6 个自然月含当月） */
const halfYearMonths = computed(() => {
  const out: { key: string; label: string; count: number }[] = []
  const d = new Date()
  d.setDate(1)
  for (let i = 5; i >= 0; i--) {
    const dd = new Date(d.getFullYear(), d.getMonth() - i, 1)
    const key = `${dd.getFullYear()}-${String(dd.getMonth() + 1).padStart(2, '0')}`
    const next = new Date(dd.getFullYear(), dd.getMonth() + 1, 1).getTime() / 1000
    const start = dd.getTime() / 1000
    const count = store.acquiredGames.filter(
      (g) => g.timeAcquired >= start && g.timeAcquired < next,
    ).length
    // 月份轴标签不是词条：走 useLocaleFormat()（zh `1月` / en `Jan`，U3）
    out.push({ key, label: fmt.month(dd), count })
  }
  return out
})
const halfYearMax = computed(() => Math.max(...halfYearMonths.value.map((m) => m.count), 1))

const kpi = computed(() => {
  const scoped = scopedGames.value
  const exclusive = scoped.filter((g) => g.ownerCount === 1).length
  const per = bars.value.length ? fmt.fixed(exclusive / bars.value.length, 1) : '0'
  const rate = scoped.length ? Math.round((exclusive / scoped.length) * 1000) / 10 : 0
  return { total: scoped.length, exclusive, per, rate }
})

/** 档位标签（图例用，独立成条）：1 人 = 独占，n 人 = N人共享 */
function tierLabel(n: number): string {
  return n === 1 ? t('famContrib.tier.exclusive') : t('famContrib.tier.shared', { n })
}

/** 档位提示（悬停气泡）：整句一条词条。把 tierLabel() 的结果塞进占位符等于
 *  拼接两段已翻译的文本，英文语序拼不回来，故单独成条。 */
function tierTip(n: number, count: number): string {
  return n === 1
    ? t('famContrib.tier.tipExclusive', { count })
    : t('famContrib.tier.tipShared', { members: n, count })
}
</script>

<template>
  <div>
    <div v-if="store.error" class="lib-empty">{{ store.error }}</div>
    <div v-else-if="store.loading && !store.ready" class="lib-empty">{{ t('famContrib.empty.loading') }}</div>
    <div v-else-if="store.games.length === 0" class="lib-empty">{{ t('famContrib.empty.noData') }}</div>

    <template v-else>
      <!-- 范围切换（累计总量/近半年） -->
      <div class="cb-range">
        <button type="button" :class="{ 'is-on': rangeMode === 'all' }" @click="rangeMode = 'all'" :title="t('famContrib.range.allTip')">{{ t('famContrib.range.all') }}</button>
        <button type="button" :class="{ 'is-on': rangeMode === 'halfyear' }" @click="rangeMode = 'halfyear'" :title="t('famContrib.range.halfYearTip')">{{ t('famContrib.range.halfYear') }}</button>
      </div>

      <div class="cb-grid">
        <!-- 左：堆叠条形图 -->
        <div class="cb-chart">
          <div class="cb-chart__title">{{ t('famContrib.chart.title') }}</div>
          <div v-for="b in bars" :key="b.sid" class="cb-row">
            <span class="cb-row__name" :title="b.name">
              <img v-if="b.avatar" class="cb-row__ava" :src="b.avatar" alt="" loading="lazy" />
              <span v-else class="cb-row__ava cb-row__ava--fb">{{ b.name.slice(0, 1) }}</span>
              <span class="cb-row__name-text">{{ b.name }}</span>
            </span>
            <div class="cb-row__track">
              <span
                v-for="(v, ti) in b.tiers"
                v-show="v > 0"
                :key="ti"
                class="cb-row__seg"
                :style="{ width: (v / barMax * 100) + '%', background: TIER_COLORS[ti % TIER_COLORS.length] }"
                :title="tierTip(ti + 1, v)"
              ></span>
            </div>
            <span class="cb-row__val">{{ b.total }}</span>
          </div>
          <!-- 图例（档位） -->
          <div class="cb-legend">
            <span v-for="ti in maxOwners" :key="ti" class="cb-legend__item">
              <i :style="{ background: TIER_COLORS[(ti - 1) % TIER_COLORS.length] }"></i>{{ tierLabel(ti) }}
            </span>
          </div>
        </div>

        <!-- 右：环形图 + 近半年增量 -->
        <div class="cb-side">
          <div class="cb-card">
            <div class="cb-card__title">{{ t('famContrib.donut.title') }}</div>
            <div class="cb-donut">
              <svg viewBox="0 0 130 130">
                <circle cx="65" cy="65" r="52" fill="none" stroke="var(--surface-chip-2)" stroke-width="14" />
                <circle
                  v-for="s in donutSegments"
                  :key="s.name"
                  cx="65" cy="65" r="52" fill="none"
                  :stroke="s.color" stroke-width="14"
                  :stroke-dasharray="s.dasharray"
                  :stroke-dashoffset="s.dashoffset"
                  transform="rotate(-90 65 65)"
                />
                <text x="65" y="62" text-anchor="middle" class="cb-donut__num">{{ donutTotal }}</text>
                <text x="65" y="76" text-anchor="middle" class="cb-donut__lbl">{{ t('famContrib.donut.total') }}</text>
              </svg>
              <div class="cb-donut__list">
                <div v-for="s in donutSegments" :key="s.name" class="cb-donut__row">
                  <i :style="{ background: s.color }"></i>
                  <img v-if="s.avatar" class="cb-donut__ava" :src="s.avatar" alt="" loading="lazy" />
                  <span v-else class="cb-donut__ava cb-donut__ava--fb">{{ s.name.slice(0, 1) }}</span>
                  <span class="cb-donut__name">{{ s.name }}</span>
                  <span class="cb-donut__val">{{ s.count }} · {{ s.pct }}%</span>
                </div>
              </div>
            </div>
          </div>

          <div class="cb-card">
            <div class="cb-card__title">{{ t('famContrib.halfYear.title') }}</div>
            <div class="cb-hy">
              <div v-for="m in halfYearMonths" :key="m.key" class="cb-hy__col" :title="t('famContrib.halfYear.tip', { month: m.key, count: m.count })">
                <span class="cb-hy__num">{{ m.count || '' }}</span>
                <span class="cb-hy__bar" :style="{ height: Math.max(4, m.count / halfYearMax * 64) + 'px' }"></span>
                <span class="cb-hy__lbl">{{ m.label }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- KPI 行 -->
      <div class="cb-kpis">
        <HlStat :label="t('famContrib.kpi.total')" :value="kpi.total" />
        <HlStat :label="t('famContrib.kpi.exclusive')" :value="kpi.exclusive" />
        <HlStat :label="t('famContrib.kpi.perMember')" :value="kpi.per" />
        <HlStat tone="good" :label="t('famContrib.kpi.rate')" :value="`${kpi.rate}%`" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.lib-empty { text-align: center; padding: 26px 16px; border: 1px dashed var(--border-soft); border-radius: var(--radius); background: var(--surface-inset); font-size: 12.5px; color: var(--text-secondary); }

.cb-range { display: inline-flex; gap: 2px; background: var(--surface-chip-2); border: 1px solid var(--border-soft); border-radius: 6px; padding: 2px; margin-bottom: 10px; }
.cb-range button { font-size: 10.5px; padding: 3px 10px; border-radius: 4px; border: none; background: transparent; color: var(--text-muted); cursor: pointer; font-family: inherit; transition: var(--transition); }
.cb-range button.is-on { background: var(--accent-a20); color: var(--accent); font-weight: 600; }

.cb-grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: 10px; }
@media (max-width: 760px) { .cb-grid { grid-template-columns: 1fr; } }

.cb-chart, .cb-card { background: var(--surface-inset); border: 1px solid var(--line-1); border-radius: var(--radius); padding: 10px 12px; }
.cb-chart__title, .cb-card__title { font-size: 11.5px; font-weight: 600; color: var(--text-secondary); margin-bottom: 10px; }

.cb-row { display: grid; grid-template-columns: 108px 1fr 40px; align-items: center; gap: 8px; margin-bottom: 9px; }
.cb-row__name { font-size: 11.5px; color: var(--text-primary); display: flex; align-items: center; gap: 6px; min-width: 0; }
.cb-row__ava { width: 17px; height: 17px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: var(--surface-chip-2); }
.cb-row__ava--fb { display: grid; place-items: center; font-size: 9px; font-weight: 600; color: var(--text-dim); }
.cb-row__name-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cb-row__track { display: flex; height: 14px; border-radius: 4px; background: var(--surface-chip-2); overflow: hidden; }
.cb-row__seg { height: 100%; transition: width 0.3s var(--ease-out); }
.cb-row__seg + .cb-row__seg { border-left: 1px solid var(--surface-inset); }
.cb-row__val { font-size: 11.5px; color: var(--text-secondary); font-family: var(--font-mono, monospace); text-align: right; }

.cb-legend { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--line-2); }
.cb-legend__item { display: flex; align-items: center; gap: 5px; font-size: 10px; color: var(--text-dim); }
.cb-legend__item i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }

.cb-side { display: flex; flex-direction: column; gap: 10px; }

.cb-donut { display: flex; gap: 12px; align-items: center; }
.cb-donut svg { width: 118px; height: 118px; flex-shrink: 0; }
.cb-donut__num { font-size: 18px; font-weight: 700; fill: var(--text-primary); font-family: var(--font-mono, monospace); }
.cb-donut__lbl { font-size: 8px; fill: var(--text-dim); }
.cb-donut__list { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.cb-donut__row { display: flex; align-items: center; gap: 6px; font-size: 10.5px; }
.cb-donut__row i { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.cb-donut__ava { width: 15px; height: 15px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: var(--surface-chip-2); }
.cb-donut__ava--fb { display: grid; place-items: center; font-size: 8.5px; font-weight: 600; color: var(--text-dim); }
.cb-donut__name { color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cb-donut__val { margin-left: auto; color: var(--text-dim); font-family: var(--font-mono, monospace); font-size: 10px; flex-shrink: 0; }

.cb-hy { display: flex; align-items: flex-end; justify-content: space-between; gap: 6px; padding: 4px 2px 0; }
.cb-hy__col { display: flex; flex-direction: column; align-items: center; gap: 4px; flex: 1; }
.cb-hy__num { font-size: 9.5px; color: var(--text-dim); font-family: var(--font-mono, monospace); }
.cb-hy__bar { width: 100%; max-width: 26px; border-radius: 3px 3px 0 0; background: linear-gradient(180deg, var(--accent), var(--accent-a40)); transition: height 0.3s var(--ease-out); }
.cb-hy__lbl { font-size: 9px; color: var(--text-dim); }

.cb-kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 10px; }
@media (max-width: 760px) { .cb-kpis { grid-template-columns: repeat(2, 1fr); } }
/* KPI 卡本体走 components/ui/HlStat.vue——卡片样式不再在本文件定义。
   这里只留**布局**（几列、间距），因为「一排几张卡」是各视图自己的事。 */
</style>
