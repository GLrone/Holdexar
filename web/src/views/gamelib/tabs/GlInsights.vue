<script setup lang="ts">
/* 库分析：全库口径的个性化洞察组件（纯 CSS 比例条，无 ECharts——
   不踩图表主题化/动画契约，后台标签页也不会白屏）。
   账号对比 / 类型分布 / 账号间重合 / 最近入库 / 价值榜。 */
import { computed, onMounted } from 'vue'

import { useOwnedLibStore } from '@/stores/ownedLib'
import { useI18n, useLocaleFormat } from '@/locales'
import HlEmpty from '@/components/ui/HlEmpty.vue'
import HlStat from '@/components/ui/HlStat.vue'
import { PALETTE } from '@/lib/familyColors'

const { t } = useI18n()
const fmt = useLocaleFormat()
const store = useOwnedLibStore()
onMounted(() => {
  if (!store.ready) void store.load()
})

/* 比例条分类色：固定色板按序取模（成员/档位「一眼分开」语义，同 family 模块） */
const BAR_COLORS = [
  PALETTE.teal,
  PALETTE.blue,
  PALETTE.amber,
  PALETTE.pink,
  PALETTE.violet,
  PALETTE.green,
  PALETTE.sky,
  PALETTE.lilac,
]

const kpi = computed(() => {
  let valueFen = 0
  let free = 0
  let shared = 0
  let uncrawled = 0
  for (const g of store.games) {
    if (g.cnPriceFen !== null) {
      valueFen += g.cnPriceFen
      if (g.cnPriceFen === 0) free++
    } else {
      uncrawled++
    }
    if (g.owners.length > 1) shared++
  }
  return { total: store.games.length, valueFen, free, shared, uncrawled }
})

/* ── 账号对比：按拥有款数降序（主账号徽章保留）── */
const accountBars = computed(() => {
  const rows = [...store.accounts].sort((a, b) => b.ownedCount - a.ownedCount)
  const max = Math.max(1, ...rows.map((a) => a.ownedCount))
  return rows.map((a, i) => ({
    ...a,
    name: store.accountName(a.steamid),
    avatar: store.accountAvatar(a.steamid),
    color: BAR_COLORS[i % BAR_COLORS.length],
    pct: Math.round((a.ownedCount / max) * 100),
  }))
})

/* ── 类型分布：每款游戏的主类型（第一个标签），top 8 ── */
const genreBars = computed(() => {
  const counts = new Map<string, number>()
  for (const g of store.games) {
    const genre = (g.genres || '').split(',')[0]?.trim() || ''
    const key = genre || '__unknown__'
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8)
  const max = Math.max(1, ...top.map(([, n]) => n))
  const total = store.games.length || 1
  return top.map(([key, n], i) => ({
    key,
    label: key === '__unknown__' ? t('gamelib.insight.genreUnknown') : key,
    n,
    pctOfLib: Math.round((n / total) * 100),
    color: BAR_COLORS[i % BAR_COLORS.length],
    pct: Math.round((n / max) * 100),
  }))
})

/* ── 账号间重合：单人独享 vs 多人共有 + 共有最多的游戏 ── */
const overlap = computed(() => {
  const total = store.games.length
  const sharedList = store.games
    .filter((g) => g.owners.length > 1)
    .sort((a, b) => b.owners.length - a.owners.length)
  const shared = sharedList.length
  const exclusive = total - shared
  const sharedPct = total ? Math.round((shared / total) * 100) : 0
  return {
    total,
    exclusive,
    shared,
    sharedPct,
    exclusivePct: 100 - sharedPct,
    top: sharedList.slice(0, 5).map((g) => ({
      appid: g.appid,
      name: g.name || `AppID ${g.appid}`,
      n: g.owners.length,
    })),
  }
})

/* ── 最近入库：全部拥有者记录按 addedAt 降序取 10 ── */
const recent = computed(() => {
  const rows: {
    appid: number
    name: string
    headerImage: string | null
    account: string
    date: string
    ts: number
  }[] = []
  for (const g of store.games) {
    for (const o of g.owners) {
      if (!o.addedAt) continue
      const ts = Date.parse(o.addedAt)
      rows.push({
        appid: g.appid,
        name: g.name || `AppID ${g.appid}`,
        headerImage: g.headerImage,
        account: store.accountName(o.steamid),
        date: o.addedAt.slice(0, 10),
        ts,
      })
    }
  }
  rows.sort((a, b) => b.ts - a.ts)
  return rows.slice(0, 10)
})

/* ── 价值榜：CN 现价最高的五款 ── */
const topValue = computed(() =>
  store.games
    .filter((g) => (g.cnPriceFen ?? 0) > 0)
    .sort((a, b) => (b.cnPriceFen ?? 0) - (a.cnPriceFen ?? 0))
    .slice(0, 5)
    .map((g) => ({
      appid: g.appid,
      name: g.name || `AppID ${g.appid}`,
      headerImage: g.headerImage,
      price: `¥${fmt.group((g.cnPriceFen ?? 0) / 100)}`,
    })),
)
</script>

<template>
  <div data-section="gamelib.section.insights">
    <HlEmpty v-if="store.loading && !store.ready" :text="t('gamelib.empty.loading')" icon="" />
    <HlEmpty v-else-if="store.error" :text="t('gamelib.empty.error', { err: store.error })" icon="⚠️" />
    <div v-else-if="store.games.length === 0" class="gl-lead">
      <HlEmpty :text="t('gamelib.empty.noAccounts')" />
      <div class="gl-lead__hint">{{ t('gamelib.empty.noAccountsHint') }}</div>
    </div>

    <template v-else>
      <div class="gl-kpi-row">
        <HlStat size="sm" :color="PALETTE.teal" :label="t('gamelib.owned.kpi.count')" :value="fmt.group(kpi.total)" />
        <HlStat size="sm" :color="PALETTE.gold" :label="t('gamelib.owned.kpi.value')" :value="kpi.valueFen > 0 ? `¥${fmt.group(kpi.valueFen / 100)}` : '—'" />
        <HlStat size="sm" :color="PALETTE.green" :label="t('gamelib.owned.kpi.free')" :value="fmt.group(kpi.free)" />
        <HlStat size="sm" :color="PALETTE.violet" :label="t('gamelib.owned.kpi.shared')" :value="fmt.group(kpi.shared)" />
      </div>
      <div v-if="kpi.uncrawled > 0" class="gl-note">{{ t('gamelib.insight.uncrawled', { n: fmt.group(kpi.uncrawled) }) }}</div>

      <div class="gl-modules">
        <!-- 账号对比 -->
        <div class="gl-module">
          <div class="gl-sub">
            {{ t('gamelib.insight.accountCmp') }}
            <small>{{ t('gamelib.insight.accountCmp.sub') }}</small>
          </div>
          <div v-for="a in accountBars" :key="a.steamid" class="gl-ratio">
            <div class="gl-ratio__name" :title="a.kinds?.owned ? undefined : t('gamelib.owned.syncOff')">
              <HlImg class="gl-ratio__ava" :src="a.avatar" :alt="a.name">
                <template #fallback>
                  <span class="gl-ratio__ava gl-ratio__ava--txt">{{ a.name.slice(0, 1) }}</span>
                </template>
              </HlImg>
              <span>{{ a.name }}</span>
            </div>
            <div class="gl-ratio__track">
              <div class="gl-ratio__fill" :style="{ width: a.pct + '%', background: a.color }"></div>
            </div>
            <div class="gl-ratio__val">
              {{ fmt.group(a.ownedCount) }} · {{ a.valueFen > 0 ? `¥${fmt.group(a.valueFen / 100)}` : '—' }}
            </div>
          </div>
        </div>

        <!-- 类型分布 -->
        <div class="gl-module">
          <div class="gl-sub">
            {{ t('gamelib.insight.genreDist') }}
            <small>{{ t('gamelib.insight.genreDist.sub') }}</small>
          </div>
          <div v-for="g in genreBars" :key="g.key" class="gl-ratio">
            <div class="gl-ratio__name"><span>{{ g.label }}</span></div>
            <div class="gl-ratio__track">
              <div class="gl-ratio__fill" :style="{ width: g.pct + '%', background: g.color }"></div>
            </div>
            <div class="gl-ratio__val">{{ fmt.group(g.n) }} · {{ g.pctOfLib }}%</div>
          </div>
        </div>

        <!-- 账号间重合 -->
        <div class="gl-module">
          <div class="gl-sub">
            {{ t('gamelib.insight.overlap') }}
            <small>{{ t('gamelib.insight.overlap.sub') }}</small>
          </div>
          <div class="gl-overlap-bar">
            <div class="gl-overlap-bar__seg is-exclusive" :style="{ width: overlap.exclusivePct + '%' }"></div>
            <div class="gl-overlap-bar__seg is-shared" :style="{ width: overlap.sharedPct + '%' }"></div>
          </div>
          <div class="gl-overlap-legend">
            <span class="gl-overlap-legend__item">
              <i class="is-exclusive"></i>{{ t('gamelib.insight.overlap.exclusive') }}
              <b>{{ fmt.group(overlap.exclusive) }}</b>
            </span>
            <span class="gl-overlap-legend__item">
              <i class="is-shared"></i>{{ t('gamelib.insight.overlap.shared') }}
              <b>{{ fmt.group(overlap.shared) }}</b>
            </span>
          </div>
          <div v-if="overlap.top.length" class="gl-overlap-top">
            <div class="gl-overlap-top__head">{{ t('gamelib.insight.overlap.top') }}</div>
            <router-link
              v-for="g in overlap.top"
              :key="g.appid"
              :to="`/game/${g.appid}`"
              class="gl-overlap-top__row"
            >
              <span class="gl-overlap-top__name">{{ g.name }}</span>
              <span class="gl-overlap-top__n">{{ t('gamelib.insight.owners', { n: g.n }) }}</span>
            </router-link>
          </div>
        </div>

        <!-- 价值榜 -->
        <div class="gl-module">
          <div class="gl-sub">
            {{ t('gamelib.insight.topValue') }}
            <small>{{ t('gamelib.insight.topValue.sub') }}</small>
          </div>
          <div v-for="g in topValue" :key="g.appid" class="gl-val-row">
            <HlImg class="gl-val-row__thumb" :src="g.headerImage" :alt="g.name" loading="lazy">
              <template #fallback>
                <span class="gl-val-row__thumb gl-val-row__thumb--txt">{{ g.name.slice(0, 2) }}</span>
              </template>
            </HlImg>
            <router-link :to="`/game/${g.appid}`" class="gl-val-row__name">{{ g.name }}</router-link>
            <span class="gl-val-row__price">{{ g.price }}</span>
          </div>
          <HlEmpty v-if="topValue.length === 0" size="sm" icon="" :text="t('gamelib.card.noPrice')" />
        </div>
      </div>

      <!-- 最近入库（全宽） -->
      <div class="gl-module" style="margin-top: 12px">
        <div class="gl-sub">
          {{ t('gamelib.insight.recent') }}
          <small>{{ t('gamelib.insight.recent.sub') }}</small>
        </div>
        <div v-for="r in recent" :key="r.appid + r.account + r.date" class="gl-recent-row">
          <HlImg class="gl-recent-row__thumb" :src="r.headerImage" :alt="r.name" loading="lazy">
            <template #fallback>
              <span class="gl-recent-row__thumb gl-recent-row__thumb--txt">{{ r.name.slice(0, 2) }}</span>
            </template>
          </HlImg>
          <router-link :to="`/game/${r.appid}`" class="gl-recent-row__name">{{ r.name }}</router-link>
          <span class="gl-recent-row__account">{{ r.account }}</span>
          <span class="gl-recent-row__date">{{ r.date }}</span>
        </div>
        <HlEmpty v-if="recent.length === 0" size="sm" icon="" :text="t('gamelib.empty.noGames')" />
      </div>
    </template>
  </div>
</template>

<style src="./gamelib-shared.css"></style>
<style scoped>
.gl-note {
  font-size: 11px;
  color: var(--text-dim);
  margin: -6px 0 12px;
}
.gl-ratio__ava {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
  display: grid;
  place-items: center;
}
.gl-ratio__ava--txt {
  background: var(--accent);
  color: var(--text-on-fill);
  font-size: 9px;
  font-weight: 700;
}

/* 重合双段条 */
.gl-overlap-bar {
  display: flex;
  height: 12px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--surface-track);
  margin-bottom: 8px;
}
.gl-overlap-bar__seg.is-exclusive {
  background: var(--warning);
}
.gl-overlap-bar__seg.is-shared {
  background: var(--accent);
}
.gl-overlap-legend {
  display: flex;
  gap: 16px;
  font-size: 11.5px;
  color: var(--text-secondary);
  margin-bottom: 12px;
}
.gl-overlap-legend__item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.gl-overlap-legend__item i {
  width: 9px;
  height: 9px;
  border-radius: 3px;
  display: inline-block;
}
.gl-overlap-legend__item i.is-exclusive {
  background: var(--warning);
}
.gl-overlap-legend__item i.is-shared {
  background: var(--accent);
}
.gl-overlap-legend__item b {
  font-family: var(--font-mono, monospace);
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}
.gl-overlap-top__head {
  font-size: 11px;
  color: var(--text-dim);
  margin-bottom: 6px;
}
.gl-overlap-top__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 5px 8px;
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  text-decoration: none;
  transition: var(--transition);
}
.gl-overlap-top__row:hover {
  background: var(--hover-soft);
  color: var(--accent);
}
.gl-overlap-top__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gl-overlap-top__n {
  flex-shrink: 0;
  font-size: 10.5px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

/* 价值榜行 */
.gl-val-row {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 4px 0;
}
.gl-val-row__thumb {
  width: 58px;
  height: 27px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  background: var(--surface-chip-2);
}
.gl-val-row__thumb--txt {
  font-size: 9px;
  font-weight: 700;
  color: var(--text-dim);
}
.gl-val-row__name {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-secondary);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: var(--transition);
}
.gl-val-row__name:hover {
  color: var(--accent);
}
.gl-val-row__price {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

/* 最近入库行 */
.gl-recent-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 5px 0;
  border-bottom: 1px solid var(--row-border);
  font-size: 12px;
}
.gl-recent-row:last-child {
  border-bottom: none;
}
.gl-recent-row__thumb {
  width: 58px;
  height: 27px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  background: var(--surface-chip-2);
}
.gl-recent-row__thumb--txt {
  font-size: 9px;
  font-weight: 700;
  color: var(--text-dim);
}
.gl-recent-row__name {
  flex: 1;
  min-width: 0;
  color: var(--text-secondary);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: var(--transition);
}
.gl-recent-row__name:hover {
  color: var(--accent);
}
.gl-recent-row__account {
  flex-shrink: 0;
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
  font-size: 11px;
}
.gl-recent-row__date {
  flex-shrink: 0;
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}

/* 引导（与 GlOwned 同形） */
.gl-lead {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 18px 0;
}
.gl-lead__hint {
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.7;
  max-width: 420px;
  text-align: center;
}
</style>
