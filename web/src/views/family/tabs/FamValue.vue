<script setup lang="ts">
/* 价值洞察（框架 vi-* 标准迁移）：KPI + 趋势柱 + 散点 + 热力 + 成员贡献排名。
   数据源：familyLib store（CN 现价 × 游玩时长 × 成员独占聚合）。 */
import { computed, onMounted } from 'vue'

import { HlStat } from '@/components/ui'
import { memberColor, PALETTE } from '@/lib/familyColors'
import { useI18n, useLocaleFormat } from '@/locales'
import { useFamilyStore } from '@/stores/familyLib'

const store = useFamilyStore()
const { t } = useI18n()
const fmt = useLocaleFormat()
onMounted(() => { if (!store.ready) void store.load() })

/** 库内已定价游戏（价值口径） */
const priced = computed(() => store.games.filter((g) => g.cnPriceFen !== null))

const kpi = computed(() => {
  // 原价合计（originalPrice，缺原价行回退现价——口径同 op>0）
  const originalFen = priced.value.reduce(
    (s, g) => s + (g.originalPriceFen ?? g.cnPriceFen ?? 0),
    0,
  )
  // 实付近似 = 现价合计（口径同 actualPaid = finalPrice>0 ? finalPrice : originalPrice）
  const actualFen = priced.value.reduce((s, g) => s + (g.cnPriceFen ?? 0), 0)
  const totalMin = store.libraryKpi.totalMin
  const perMember = store.members.length ? Math.round(originalFen / store.members.length) : 0
  const nowSec = Math.floor(Date.now() / 1000)
  // 近90天新增价值 = 入库时间（timeAcquired）近 90 天的原价合计
  const recent90 = priced.value.filter((g) => g.timeAcquired > nowSec - 90 * 86400)
  const recent90Fen = recent90.reduce(
    (s, g) => s + (g.originalPriceFen ?? g.cnPriceFen ?? 0),
    0,
  )
  const savingsFen = originalFen - actualFen
  const savingsPct = originalFen > 0 ? Math.round((savingsFen / originalFen) * 100) : 0
  const perHour = totalMin > 0 ? actualFen / 100 / (totalMin / 60) : 0
  return {
    originalFen,
    actualFen,
    savingsFen,
    savingsPct,
    valueFen: originalFen,
    perMemberFen: perMember,
    recent90Fen,
    perHour,
    totalMin,
    pricedCount: priced.value.length,
  }
})

/** 入库价值趋势：按入库月份聚合原价（rt_time_acquired 口径） */
const trend = computed(() => {
  const buckets = new Map<string, { fen: number; count: number }>()
  const now = new Date()
  for (let i = 11; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1)
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    buckets.set(key, { fen: 0, count: 0 })
  }
  for (const g of priced.value) {
    if (!g.timeAcquired) continue
    const d = new Date(g.timeAcquired * 1000)
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    const slot = buckets.get(key)
    if (slot) {
      slot.fen += g.originalPriceFen ?? g.cnPriceFen ?? 0
      slot.count += 1
    }
  }
  const rows = [...buckets.entries()].map(([key, v]) => ({
    key,
    fen: v.fen,
    count: v.count,
    // 月份轴标签随语言：zh-CN → 「1月」，en → 「Jan」（useLocaleFormat().month）
    label: fmt.month(new Date(Number(key.slice(0, 4)), parseInt(key.slice(5, 7), 10) - 1, 1)),
  }))
  const max = Math.max(...rows.map((r) => r.fen), 1)
  return { rows, max }
})

/** 价格 × 游玩时长散点（真实数据，取时长>0 且有价的前 40 款） */
const scatter = computed(() => {
  const list = priced.value
    .filter((g) => g.playtimeMinutes > 0)
    .sort((a, b) => b.playtimeMinutes - a.playtimeMinutes)
    .slice(0, 40)
  const maxPrice = Math.max(...list.map((g) => g.cnPriceFen ?? 0), 1)
  const maxMin = Math.max(...list.map((g) => g.playtimeMinutes), 1)
  return list.map((g) => ({
    appid: g.appid,
    name: g.name || `AppID ${g.appid}`,
    priceFen: g.cnPriceFen ?? 0,
    minutes: g.playtimeMinutes,
    left: Math.round(((g.cnPriceFen ?? 0) / maxPrice) * 92),
    bottom: Math.round((g.playtimeMinutes / maxMin) * 90),
    size: 6 + Math.round(Math.sqrt(g.playtimeMinutes / maxMin) * 12),
    color: memberColor(store.members.findIndex((m) => m.steamid === g.owners[0]) + 6),
  }))
})

/** 成员价值贡献排名（独占价值 = 该成员拥有的游戏的 CN 现价合计） */
const memberRank = computed(() => {
  const rows = store.memberStats.map((m, i) => ({
    ...m,
    color: memberColor(i),
    fen: m.valueFen,
  }))
  const max = Math.max(...rows.map((r) => r.fen), 1)
  return rows
    .map((r) => ({ ...r, pct: Math.round((r.fen / max) * 100) }))
    .sort((a, b) => b.fen - a.fen)
})

/** 价格区间 × 月份 活跃热力（真实 lastPlayed 聚合，密度 0-4） */
function bandOf(fen: number | null): number {
  if (fen === null) return 5
  if (fen === 0) return 0
  if (fen < 5000) return 1
  if (fen < 10000) return 2
  if (fen < 20000) return 3
  return 4
}
/* 分档常量表只存 key（存译文＝把语言冻在加载那一刻），译文在 computed 里现取。
   与 FamWish 的 PRICE_BANDS 是同一套分档，但中文写法不同（此处 ¥200+、那边 ≥¥200）
   且此处多一档「未定价」——中文保持原样，英文两侧逐字一致（见词典注释）。 */
const BAND_KEYS = [
  'famValue.band.free',
  'famValue.band.lt50',
  'famValue.band.from50to100',
  'famValue.band.from100to200',
  'famValue.band.gte200',
  'famValue.band.unpriced',
] as const
const bandLabels = computed(() => BAND_KEYS.map((k) => t(k)))
const heat = computed(() => {
  // 12 个月桶
  const now = new Date()
  const keys: string[] = []
  for (let i = 11; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1)
    keys.push(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`)
  }
  // counts[band][month]
  const counts: number[][] = Array.from({ length: 6 }, () => Array(keys.length).fill(0))
  for (const g of store.games) {
    if (!g.lastPlayed) continue
    const d = new Date(g.lastPlayed * 1000)
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    const mi = keys.indexOf(key)
    if (mi >= 0) counts[bandOf(g.cnPriceFen)][mi] += 1
  }
  // 密度分档 0-4（相对该行最大值）
  return counts.map((row) => {
    const max = Math.max(...row, 1)
    return row.map((v) => (v === 0 ? 0 : Math.min(4, Math.ceil((v / max) * 4))))
  })
})
</script>

<template>
  <div>
    <div v-if="store.error" class="lib-empty">{{ store.error }}</div>
    <div v-else-if="store.loading && !store.ready" class="lib-empty">{{ t('famValue.empty.loading') }}</div>
    <div v-else-if="store.games.length === 0" class="lib-empty">{{ t('famValue.empty.noData') }}</div>

    <template v-else>
      <!-- KPI 卡的标签/副标题：文案走 t()，金额走 useLocaleFormat()（金额在组件侧
           格式化后作为参数传入，词条里不放已格式化的数字串）。
           第 1 张卡的副标题是「实付 ¥x · 省 N%」一条整句，故整句进词条 + v-html
           渲染——那抹绿取自色板 PALETTE.green，由组件侧当参数传入，
           词典里不写裸十六进制（familyColors.ts 的约定）。 -->
      <div class="vi-kpi-row">
        <HlStat
          :color="PALETTE.teal"
          :style="{ borderColor: PALETTE.teal + '30' }"
          :label="t('famValue.kpi.libraryValue')"
          :value="`¥${fmt.group(kpi.originalFen / 100)}`"
        >
          <template #sub>
            <span v-html="t('famValue.kpi.paidSave', { paid: fmt.group(kpi.actualFen / 100), pct: kpi.savingsPct, color: PALETTE.green })"></span>
          </template>
        </HlStat>
        <HlStat
          :color="PALETTE.blue"
          :style="{ borderColor: PALETTE.blue + '30' }"
          :label="t('famValue.kpi.perMember')"
          :value="`¥${fmt.group(kpi.perMemberFen / 100)}`"
          :sub="t('famValue.kpi.perMemberSub', { n: store.members.length })"
        />
        <HlStat
          :color="PALETTE.amber"
          :style="{ borderColor: PALETTE.amber + '30' }"
          :label="t('famValue.kpi.recent90')"
          :value="`¥${fmt.group(kpi.recent90Fen / 100)}`"
          :sub="t('famValue.kpi.recent90Sub')"
        />
        <HlStat
          :color="PALETTE.lilac"
          :style="{ borderColor: PALETTE.lilac + '30' }"
          :label="t('famValue.kpi.perHour')"
          :value="`¥${kpi.perHour.toFixed(1)}/h`"
          :sub="t('famValue.kpi.perHourSub', { h: (kpi.totalMin / 60).toFixed(0) })"
        />
      </div>

      <div class="fx-grid">
        <div class="vi-card">
          <div class="vi-card-title">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#06cfbe" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
            {{ t('famValue.trend.title') }}
          </div>
          <div class="vi-bars">
            <div v-for="r in trend.rows" :key="r.key" class="vi-bars-col" :title="t('famValue.trend.tooltip', { month: r.key, n: r.count, amt: (r.fen / 100).toFixed(0) })">
              <div class="vi-bars-bar" :style="{ height: Math.max(2, (r.fen / trend.max) * 88) + '%' }"></div>
              <span class="vi-bars-lbl">{{ r.label }}</span>
            </div>
          </div>
          <div style="font-size: 10px; color: var(--text-dim); margin-top: 6px; line-height: 1.6">
            {{ t('famValue.trend.note') }}
          </div>
        </div>
        <div class="vi-card">
          <div class="vi-card-title">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#a78bfa" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M3 3v18h18"/></svg>
            {{ t('famValue.scatter.title') }}
          </div>
          <div class="vi-scatter">
            <span style="position: absolute; left: -2px; top: -14px; font-size: 9px; color: var(--text-dim)">{{ t('famValue.scatter.yAxis') }}</span>
            <span style="position: absolute; right: 0; bottom: -16px; font-size: 9px; color: var(--text-dim)">{{ t('famValue.scatter.xAxis') }}</span>
            <div
              v-for="d in scatter"
              :key="d.appid"
              class="vi-scatter-dot"
              :style="{ left: d.left + '%', bottom: d.bottom + '%', width: d.size + 'px', height: d.size + 'px', background: d.color }"
              :title="t('famValue.scatter.dot', { name: d.name, price: `¥${(d.priceFen / 100).toFixed(0)}`, h: (d.minutes / 60).toFixed(1) })"
            ></div>
            <div v-if="scatter.length === 0" class="fx-empty-mini" style="position: static">{{ t('famValue.empty.noPlaytime') }}</div>
          </div>
          <div style="font-size: 10px; color: var(--text-dim); margin-top: 14px; line-height: 1.7">
            {{ t('famValue.scatter.note') }}
          </div>
        </div>
      </div>

      <div class="fx-grid" style="margin-top: 10px">
        <div class="vi-card">
          <div class="vi-card-title">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" :stroke="PALETTE.amber" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
            {{ t('famValue.heat.title') }}
          </div>
          <div style="display: grid; grid-template-columns: 56px repeat(12, 1fr); gap: 2px; align-items: center">
            <span style="font-size: 9px; color: var(--text-dim)">{{ t('famValue.heat.rowLabel') }}</span>
            <span v-for="r in trend.rows" :key="r.key" style="font-size: 8px; color: var(--text-dim); text-align: center">{{ r.label }}</span>
            <template v-for="(row, ri) in heat" :key="ri">
              <span style="font-size: 9.5px; color: var(--text-secondary)">{{ bandLabels[ri] }}</span>
              <div v-for="(lv, ci) in row" :key="ci" class="vi-heat-cell" :style="{ background: `var(--gh-${lv})` }"></div>
            </template>
          </div>
          <div class="gh-legend" style="margin-top: 8px; justify-content: flex-start">{{ t('famValue.heat.low') }} <i style="background: var(--gh-0)"></i><i style="background: var(--gh-1)"></i><i style="background: var(--gh-2)"></i><i style="background: var(--gh-3)"></i><i style="background: var(--gh-4)"></i> {{ t('famValue.heat.high') }}</div>
        </div>
        <div class="vi-card">
          <div class="vi-card-title">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#54a0ff" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>
            {{ t('famValue.rank.title') }}
          </div>
          <div v-if="memberRank.length === 0" class="fx-empty-mini">{{ t('famValue.empty.noMemberData') }}</div>
          <div v-for="m in memberRank" :key="m.steamid" class="vi-member-row">
            <HlImg :src="m.avatarUrl" class="vi-member-img" alt="" loading="lazy">
              <template #fallback>
                <div class="vi-member-ava" :style="{ background: `linear-gradient(135deg, ${m.color}, ${m.color}cc)` }">{{ (m.personaName || m.steamid.slice(-4)).slice(0, 1) }}</div>
              </template>
            </HlImg>
            <span class="vi-member-name">{{ m.personaName || t('famValue.rank.memberFallback', { id: m.steamid.slice(-4) }) }}</span>
            <div class="vi-member-bar"><div class="vi-member-fill" :style="{ width: m.pct + '%', background: `linear-gradient(90deg, ${m.color}, ${m.color}cc)` }"></div></div>
            <span class="vi-member-val">¥{{ fmt.group(m.fen / 100) }}</span>
          </div>
          <!-- 整句一条词条（行内 <b> 写在词条值里），v-html 渲染 -->
          <div
            class="vi-rank-note"
            style="margin-top: 10px; padding: 8px 10px; background: var(--surface-chip-2); border-radius: 6px; font-size: 10.5px; color: var(--text-muted); line-height: 1.7"
            v-html="t('famValue.rank.note')"
          ></div>
        </div>
      </div>
    </template>
  </div>
</template>

<style src="./tabs-shared.css"></style>
<style scoped>
.lib-empty { text-align: center; padding: 26px 16px; border: 1px dashed var(--border-soft); border-radius: var(--radius); background: var(--surface-inset); font-size: 12.5px; color: var(--text-secondary); }
.vi-kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 10px; }
/* KPI 卡本体走 components/ui/HlStat.vue。卡片的**边框染色**仍留在这里——
   那是分类色板的用法（四张卡四色），经 attr 落到 HlStat 根节点，不是重复定义。 */
.vi-card { background: var(--surface-inset); border: 1px solid var(--line-1); border-radius: 8px; padding: 12px; }
.vi-card-title { font-size: 13px; font-weight: 600; color: var(--text-primary); margin-bottom: 10px; display: flex; align-items: center; gap: 6px; }
.vi-bars { display: flex; align-items: flex-end; gap: 5px; height: 150px; padding-top: 8px; }
.vi-bars-col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 5px; height: 100%; justify-content: flex-end; }
.vi-bars-bar { width: 100%; max-width: 24px; border-radius: 4px 4px 0 0; background: linear-gradient(180deg, var(--accent-fill), var(--accent)); transition: height 0.8s var(--ease-out); }
.vi-bars-bar:hover { filter: brightness(1.12); }
.vi-bars-lbl { font-size: 9px; color: var(--text-dim); font-family: var(--font-mono, monospace); }
.vi-scatter { position: relative; height: 200px; border-left: 1px solid var(--line-2); border-bottom: 1px solid var(--line-2); }
.vi-scatter-dot { position: absolute; border-radius: 50%; opacity: 0.7; transition: opacity 0.2s, transform 0.2s; cursor: pointer; }
.vi-scatter-dot:hover { opacity: 1; transform: scale(1.4); z-index: 2; }
.vi-heat-cell { height: 18px; border-radius: 3px; transition: transform 0.15s; cursor: pointer; }
.vi-heat-cell:hover { transform: scale(1.15); outline: 1.5px solid var(--accent); }
.vi-member-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.vi-member-img { width: 24px; height: 24px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
.vi-member-ava { width: 24px; height: 24px; border-radius: 50%; display: grid; place-items: center; font-size: 11px; font-weight: 700; color: var(--text-on-fill); flex-shrink: 0; }
.vi-member-name { width: 52px; font-size: 11px; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }
.vi-member-bar { flex: 1; height: 10px; background: var(--surface-track); border-radius: 5px; overflow: hidden; position: relative; }
.vi-member-fill { height: 100%; border-radius: 5px; transition: width 0.7s var(--ease-out); }
.vi-member-val { width: 64px; text-align: right; font-size: 10.5px; font-weight: 600; font-family: var(--font-mono, monospace); color: var(--text-primary); flex-shrink: 0; }
/* :deep() —— 这行的 <b> 由 v-html 注入，拿不到 scoped 属性（同 FamPlay 的 .pa-member-sub）。 */
.vi-rank-note :deep(b) { color: var(--text-primary); }
.gh-legend { display: flex; align-items: center; gap: 4px; font-size: 10px; color: var(--text-dim); font-family: var(--font-mono, monospace); }
.gh-legend i { width: 11px; height: 11px; border-radius: 2px; display: inline-block; }
</style>
