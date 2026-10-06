<script setup lang="ts">
import { computed, ref } from 'vue'

import { formatCnyFen } from '@/api/regions'
import type {
  PilotActionFacts,
  PilotFacts,
  PilotGameFactsItem,
  PilotNavigateFacts,
  PilotPriceFacts,
  PilotProposalFacts,
  PilotRowsFacts,
} from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import { pilotCoverUrl, positivePct, ratingClass } from '@/lib/pilotView'
import HlButton from '@/components/ui/HlButton.vue'
import HlImg from '@/components/ui/HlImg.vue'
import CurrencyFlag from '@/components/CurrencyFlag.vue'
import RegionFlag from '@/components/RegionFlag.vue'
import HlPilotCompare from '@/components/business/HlPilotCompare.vue'
import HlPilotRegions from '@/components/business/HlPilotRegions.vue'
import HlPilotStepper from '@/components/business/HlPilotStepper.vue'
import HlPilotTrend from '@/components/business/HlPilotTrend.vue'

const props = defineProps<{
  cards: PilotFacts[]
  busyPid?: string | null
}>()

const emit = defineEmits<{
  (e: 'proposal', payload: { pid: string; approve: boolean }): void
}>()

const { t, locale } = useI18n()

const visible = computed(() => (props.cards || []).filter(Boolean))
const expanded = ref(false)
const displayedCards = computed(() => {
  if (expanded.value || visible.value.length <= 2) return visible.value
  return visible.value.slice(0, 2)
})

function fen(v: number | null | undefined): string {
  return typeof v === 'number' && v > 0 ? formatCnyFen(v) : '—'
}

function ratingText(rate: number | null | undefined): string | null {
  const pct = positivePct(rate)
  return pct === null ? null : `${pct}%`
}

const countFmt = new Map<string, Intl.NumberFormat>()
function fmtCount(n: number | null | undefined): string {
  if (!n) return ''
  const loc = locale.value
  let f = countFmt.get(loc)
  if (!f) {
    f = new Intl.NumberFormat(loc, { notation: 'compact' })
    countFmt.set(loc, f)
  }
  return f.format(n)
}

function dateOf(iso: string | null | undefined): string {
  return (iso || '').slice(0, 10)
}

function rangePos(f: PilotPriceFacts): number | null {
  const cur = f.cn?.cnyFen
  const y = f.year
  if (!y || y.maxFen <= y.minFen || typeof cur !== 'number' || cur <= 0) return null
  return Math.max(0, Math.min(100, Math.round(((cur - y.minFen) / (y.maxFen - y.minFen)) * 100)))
}

function medianPos(f: PilotPriceFacts): number | null {
  const y = f.year
  if (!y || y.maxFen <= y.minFen) return null
  return Math.max(0, Math.min(100, Math.round(((y.medianFen - y.minFen) / (y.maxFen - y.minFen)) * 100)))
}

function navModuleName(target: string): string {
  return t(`pilot.nav.${target}` as MessageKey)
}

function actionReceipt(f: PilotActionFacts): string {
  if (f.action === 'monitor_add') return t('pilot.action.monitor', { name: f.name ?? '—' })
  if (f.targetType === 'historic_low') return t('pilot.action.alertLow', { name: f.name ?? '—' })
  return t('pilot.action.alertPrice', { name: f.name ?? '—', price: fen(f.targetValueFen) })
}

function gameItems(c: PilotFacts): PilotGameFactsItem[] {
  return c.kind === 'games' ? c.items : []
}

const activeGameFilters = ref<Record<number, string>>({})

function getActiveFilter(cardIndex: number): string {
  return activeGameFilters.value[cardIndex] || 'all'
}

function setGameFilter(cardIndex: number, filterKey: string) {
  activeGameFilters.value = {
    ...activeGameFilters.value,
    [cardIndex]: filterKey,
  }
}

interface FilterChipDef {
  key: string
  labelKey: MessageKey
  count: number
}

function getFilterChips(c: PilotFacts): FilterChipDef[] {
  const items = gameItems(c)
  if (items.length < 2) return []

  const chips: FilterChipDef[] = [
    { key: 'all', labelKey: 'pilot.filter.all', count: items.length },
  ]

  const rating90Count = items.filter((g) => {
    const p = positivePct(g.positiveRate)
    return p !== null && p >= 90
  }).length
  if (rating90Count > 0) {
    chips.push({ key: 'rating90', labelKey: 'pilot.filter.rating90', count: rating90Count })
  }

  const discCount = items.filter((g) => (g.discount || 0) > 0).length
  if (discCount > 0) {
    chips.push({ key: 'discounted', labelKey: 'pilot.filter.discounted', count: discCount })
  }

  const budget50Count = items.filter((g) => typeof g.cnyFen === 'number' && g.cnyFen > 0 && g.cnyFen <= 5000).length
  if (budget50Count > 0) {
    chips.push({ key: 'budget50', labelKey: 'pilot.filter.budget50', count: budget50Count })
  }

  const cnCount = items.filter((g) => hasSimplifiedChinese(g.chineseSupport)).length
  if (cnCount > 0) {
    chips.push({ key: 'chinese', labelKey: 'pilot.filter.chinese', count: cnCount })
  }

  return chips
}

function hasSimplifiedChinese(support: string | null | undefined): boolean {
  if (!support) return false
  return /(?:schinese|simplified|[\u7b80])/i.test(support)
}

function filteredGameItems(c: PilotFacts, cardIndex: number): PilotGameFactsItem[] {
  const items = gameItems(c)
  const filterKey = getActiveFilter(cardIndex)
  if (filterKey === 'all') return items

  if (filterKey === 'rating90') {
    return items.filter((g) => {
      const p = positivePct(g.positiveRate)
      return p !== null && p >= 90
    })
  }
  if (filterKey === 'discounted') {
    return items.filter((g) => (g.discount || 0) > 0)
  }
  if (filterKey === 'budget50') {
    return items.filter((g) => typeof g.cnyFen === 'number' && g.cnyFen > 0 && g.cnyFen <= 5000)
  }
  if (filterKey === 'chinese') {
    return items.filter((g) => hasSimplifiedChinese(g.chineseSupport))
  }
  return items
}

function proposalTitle(c: PilotProposalFacts): string {
  const count = c.items.length
  if (c.action === 'delete') return t('pilot.proposal.delete', { count })
  if (c.action === 'add_follow') return t('pilot.proposal.follow', { count })
  if (c.args?.target_type === 'price') {
    const yuan = c.args.target_value_yuan
    return t('pilot.proposal.alertPrice', { count, price: typeof yuan === 'number' ? fen(yuan * 100) : '—' })
  }
  return t('pilot.proposal.alertLow', { count })
}

function proposalResult(c: PilotProposalFacts): string {
  if (c.state === 'rejected') return t('pilot.proposal.rejected')
  if (c.state === 'withdrawn') return t('pilot.proposal.withdrawn')
  if (c.state === 'dismissed') return t('pilot.proposal.dismissed')
  const done = c.done ?? c.items.length
  const text = t('pilot.proposal.done', { done })
  return c.failedCount ? `${text} · ${t('pilot.proposal.failed', { count: c.failedCount })}` : text
}

type RowsRow = PilotRowsFacts['rows'][number]

const imeComposing = ref(false)
const imeEnded = ref(false)

function proposalKeydown(c: PilotProposalFacts, e: KeyboardEvent) {
  if (c.state !== 'pending' || props.busyPid === c.pid) return
  const el = e.target as Element
  if (e.defaultPrevented || !e.currentTarget.contains(document.activeElement)) return
  if (el.closest('input, textarea, select, [contenteditable="true"], [contenteditable=""]')) return
  if (e.key !== 'Enter' && e.key !== 'Escape') return
  if (e.key === 'Enter' && el.closest('button, a[href], [role="button"]')) return
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return
  e.preventDefault()
  e.stopPropagation()
  if (e.repeat || imeComposing.value || imeEnded.value || e.isComposing || e.keyCode === 229) return
  emit('proposal', { pid: c.pid, approve: e.key === 'Enter' })
}

function steamFriendCode(sid: string): string {
  try {
    const n = BigInt(sid)
    if (n > 76561197960265728n) {
      return String(n - 76561197960265728n)
    }
  } catch {
    // 格式异常兜底
  }
  return sid ? sid.slice(-6) : '—'
}

function familyMemberName(m: { steamid: string; name?: string | null }): string {
  return m.name || t('family.member.unnamed', { id: m.steamid.slice(-4) })
}

function rowLabel(r: RowsRow): string {
  return r.kindKey ? t(r.kindKey as MessageKey) : r.k
}

function rowValue(r: RowsRow): string {
  if (!r.vKey) return ''
  const data: Record<string, unknown> = { ...(r.data ?? {}) }
  for (const [key, val] of Object.entries(data)) {
    if (key.endsWith('Fen') && typeof val === 'number' && val > 0) {
      data[key.slice(0, -3)] = fen(val)
    }
  }
  return t(`pilot.row.${r.vKey}` as MessageKey, data)
}

function noteText(n: NonNullable<PilotGameFactsItem['note']>): string {
  const parts: string[] = []
  const main = t(`pilot.row.${n.key}` as MessageKey)
  if (main) parts.push(main)
  if (n.v) parts.push(n.v)
  const d = dateOf(n.at ?? null)
  if (d) parts.push(d)
  return parts.join(' · ')
}

function noteTone(key: string): string | null {
  return key === 'ev_new_historical_low' || key === 'ev_price_drop' ? 'is-ok' : null
}

interface DealAdvice {
  tone: 'success' | 'accent' | 'warning' | 'muted'
  labelKey: MessageKey
}

function getDealAdvice(c: PilotPriceFacts): DealAdvice | null {
  const cur = c.cn?.cnyFen
  if (cur == null) {
    if (c.alt) return { tone: 'muted', labelKey: 'pilot.card.advice.altOnly' }
    return null
  }
  const discount = c.cn?.discount ?? 0
  const lowest = c.lowest?.cnyFen
  const yearMin = c.year?.minFen
  const median = c.year?.medianFen

  if (lowest != null && cur <= lowest && discount > 0) {
    if (yearMin != null && cur < yearMin) {
      return { tone: 'success', labelKey: 'pilot.card.advice.newLow' }
    }
    return { tone: 'success', labelKey: 'pilot.card.advice.atl' }
  }
  if (lowest != null && cur > lowest && discount > 0) {
    if (discount >= 40 || (median != null && cur <= median)) {
      return { tone: 'accent', labelKey: 'pilot.card.advice.goodDeal' }
    }
    return { tone: 'warning', labelKey: 'pilot.card.advice.regular' }
  }
  if (discount === 0) {
    return { tone: 'muted', labelKey: 'pilot.card.advice.fullPrice' }
  }
  return null
}

const expandedTrends = ref<Record<number, boolean>>({})

function isTrendExpanded(appid: number): boolean {
  return expandedTrends.value[appid] ?? false
}

function toggleTrend(appid: number) {
  expandedTrends.value = {
    ...expandedTrends.value,
    [appid]: !isTrendExpanded(appid),
  }
}
</script>

<template>
  <div v-if="visible.length" class="pcards-container">
    <div
      v-for="(c, ci) in displayedCards"
      :key="ci"
      class="pcard"
      :class="`pcard--${c.kind}`"
    >
      <!-- 价格卡：Steam 视觉展台（头部海报 + 核心大价 + 基准对比矩阵 + 水位标尺 + 走势图） -->
      <template v-if="c.kind === 'price'">
        <div class="pcard-hero">
          <HlImg :src="pilotCoverUrl(c.appid)" :alt="c.name || `AppID ${c.appid}`" loading="lazy" class="pcard-hero__cover">
            <template #fallback>
              <span class="pcard-hero__cover-fallback">{{ (c.name || `AppID ${c.appid}`).slice(0, 2) }}</span>
            </template>
          </HlImg>

          <div class="pcard-hero__info">
            <div class="pcard-hero__title-row">
              <router-link class="pcard-hero__title" :to="`/game/${c.appid}`" :title="c.name || `AppID ${c.appid}`">
                {{ c.name || `AppID ${c.appid}` }}
                <svg class="pcard-hero__title-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M7 17l9.2-9.2M17 17V8H8" />
                </svg>
              </router-link>
            </div>

            <div class="pcard-hero__badges">
              <span
                v-if="ratingText(c.positiveRate)"
                class="pcard-pill pcard-pill--rating"
                :class="ratingClass(c.positiveRate)"
              >
                <i class="pcard-pill__dot" aria-hidden="true"></i>
                {{ t('pilot.card.rating', { rate: ratingText(c.positiveRate) }) }}
                <template v-if="c.reviewCount"> · {{ fmtCount(c.reviewCount) }}</template>
              </span>

              <span
                v-if="getDealAdvice(c)"
                class="pcard-pill pcard-pill--advice"
                :class="`is-${getDealAdvice(c)!.tone}`"
              >
                <i class="pcard-pill__spark" aria-hidden="true"></i>
                {{ t(getDealAdvice(c)!.labelKey) }}
              </span>
            </div>
          </div>
        </div>

        <!-- 价格核心展示区 -->
        <div class="pcard-price-board">
          <div class="pcard-price-board__primary">
            <div class="pcard-price-board__now">
              <span class="pcard-price-board__currency">¥</span>
              <span class="pcard-price-board__val">{{ fen(c.cn?.cnyFen).replace(/[¥￥]/g, '') }}</span>
            </div>
            <span v-if="c.cn?.discount" class="pcard-disc-tag">
              -{{ c.cn.discount }}%
            </span>
          </div>

          <div v-if="c.cn?.cnyFen == null && c.alt" class="pcard-price-board__alt">
            <span class="pcard-price-board__alt-label">{{ t('pilot.card.altRegion', { region: c.alt.region }) }}</span>
            <span class="pcard-price-board__alt-val">{{ fen(c.alt.cnyFen) }}</span>
            <span v-if="c.alt.discount" class="pcard-disc-tag pcard-disc-tag--sm">-{{ c.alt.discount }}%</span>
          </div>
        </div>

        <!-- 历史价格基准指标面板 -->
        <div class="pcard-metrics-grid">
          <div class="pcard-metric-card">
            <span class="pcard-metric-card__k">{{ t('pilot.card.atl') }}</span>
            <span class="pcard-metric-card__v is-highlight">{{ fen(c.lowest?.cnyFen) }}</span>
            <span v-if="dateOf(c.lowest?.snapshotAt)" class="pcard-metric-card__sub">{{ dateOf(c.lowest?.snapshotAt) }}</span>
          </div>

          <div v-if="c.year" class="pcard-metric-card">
            <span class="pcard-metric-card__k">{{ t('pilot.card.medianTick') }}</span>
            <span class="pcard-metric-card__v">{{ fen(c.year.medianFen) }}</span>
            <span class="pcard-metric-card__sub">{{ t('pilot.card.trendRange', { min: fen(c.year.minFen), max: fen(c.year.maxFen) }) }}</span>
          </div>

          <div v-if="c.alt && c.cn?.cnyFen != null" class="pcard-metric-card">
            <span class="pcard-metric-card__k">{{ t('pilot.card.advice.altOnly') }}</span>
            <span class="pcard-metric-card__v">{{ fen(c.alt.cnyFen) }}</span>
            <span class="pcard-metric-card__sub">{{ c.alt.region }}<template v-if="c.alt.discount"> · -{{ c.alt.discount }}%</template></span>
          </div>
        </div>

        <!-- 价格水位区间标尺 -->
        <div v-if="rangePos(c) !== null" class="pcard-gauge">
          <div class="pcard-gauge__header">
            <span class="pcard-gauge__tip">{{ fen(c.year?.minFen) }}</span>
            <span class="pcard-gauge__tip">{{ fen(c.year?.maxFen) }}</span>
          </div>
          <div class="pcard-gauge__track">
            <i
              class="pcard-gauge__tick"
              :style="{ left: `${medianPos(c)}%` }"
              :title="t('pilot.card.medianTick')"
            ></i>
            <i class="pcard-gauge__dot" :style="{ left: `${rangePos(c)}%` }"></i>
          </div>
        </div>

        <!-- 折叠价格走势与统计说明 -->
        <div v-if="(c.trend && c.trend.length >= 2) || c.year" class="pcard-trend-wrap">
          <button
            type="button"
            class="pcard-trend-toggle"
            :aria-expanded="isTrendExpanded(c.appid)"
            @click="toggleTrend(c.appid)"
          >
            <svg class="pcard-trend-toggle__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <polyline points="23 6 13.5 15.5 8.5 10.5 1 18" />
              <polyline points="17 6 23 6 23 12" />
            </svg>
            <span>{{ t(isTrendExpanded(c.appid) ? 'pilot.card.hideTrend' : 'pilot.card.toggleTrend') }}</span>
            <svg class="pcard-trend-toggle__chevron" :class="{ 'is-open': isTrendExpanded(c.appid) }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </button>
          <div v-if="isTrendExpanded(c.appid)" class="pcard-trend-content">
            <HlPilotTrend v-if="c.trend && c.trend.length >= 2" :points="c.trend" />
            <div v-if="c.year" class="pcard-trend-footnote">
              {{ t('pilot.card.year', { min: fen(c.year.minFen), max: fen(c.year.maxFen), median: fen(c.year.medianFen) }) }}
              <template v-if="c.year.count"> · {{ t('pilot.card.obs', { count: c.year.count }) }}</template>
            </div>
          </div>
        </div>
      </template>

      <!-- 地区比价卡 -->
      <HlPilotRegions v-else-if="c.kind === 'regions'" :card="c" />

      <!-- 游戏对比卡 -->
      <HlPilotCompare v-else-if="c.kind === 'compare'" :card="c" />

      <!-- 游戏清单与候选推荐卡 -->
      <template v-else-if="c.kind === 'games'">
        <div class="pcard-header-bar">
          <div class="pcard-header-bar__title">
            <svg class="pcard-header-bar__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <rect x="2" y="3" width="20" height="14" rx="2" />
              <line x1="8" y1="21" x2="16" y2="21" />
              <line x1="12" y1="17" x2="12" y2="21" />
            </svg>
            <span>{{ c.titleKey ? t(`pilot.rows.${c.titleKey}` as MessageKey) : t('pilot.facts.gamesTitle') }}</span>
          </div>
          <span v-if="c.total != null && c.total > c.items.length" class="pcard-header-bar__badge">
            {{ t('pilot.facts.more', { n: c.total }) }}
          </span>
        </div>

        <div v-if="getFilterChips(c).length > 1" class="pcard-filter-pills">
          <button
            v-for="chip in getFilterChips(c)"
            :key="chip.key"
            type="button"
            class="pcard-filter-pill"
            :class="{ 'is-active': getActiveFilter(ci) === chip.key }"
            @click="setGameFilter(ci, chip.key)"
          >
            <span>{{ t(chip.labelKey) }}</span>
            <span class="pcard-filter-pill__count">{{ chip.count }}</span>
          </button>
        </div>

        <div v-if="filteredGameItems(c, ci).length === 0" class="pcard-empty-state">
          <span>{{ t('pilot.filter.empty') }}</span>
          <button type="button" class="pcard-empty-state__reset" @click="setGameFilter(ci, 'all')">
            {{ t('pilot.filter.reset') }}
          </button>
        </div>

        <div v-else class="pcard-game-list">
          <router-link
            v-for="(g, gi) in filteredGameItems(c, ci)"
            :key="g.appid"
            class="pcard-game-item"
            :to="`/game/${g.appid}`"
          >
            <span class="pcard-game-item__index">{{ gi + 1 }}</span>
            <HlImg :src="pilotCoverUrl(g.appid)" :alt="g.name || `AppID ${g.appid}`" loading="lazy" class="pcard-game-item__cover">
              <template #fallback>
                <span class="pcard-game-item__fallback">{{ (g.name || `AppID ${g.appid}`).slice(0, 2) }}</span>
              </template>
            </HlImg>

            <div class="pcard-game-item__content">
              <div class="pcard-game-item__top">
                <span class="pcard-game-item__title" :title="g.name || `AppID ${g.appid}`">{{ g.name || `AppID ${g.appid}` }}</span>
                <span v-if="hasSimplifiedChinese(g.chineseSupport)" class="pcard-cn-tag">
                  {{ t('pilot.filter.chinese') }}
                </span>
              </div>

              <div class="pcard-game-item__bottom">
                <span v-if="ratingText(g.positiveRate)" class="pcard-game-item__rating" :class="ratingClass(g.positiveRate)">
                  {{ ratingText(g.positiveRate) }}
                </span>
                <span class="pcard-game-item__price-block">
                  <span class="pcard-game-item__price">{{ fen(g.cnyFen) }}</span>
                  <span v-if="g.discount" class="pcard-disc-tag pcard-disc-tag--xs">-{{ g.discount }}%</span>
                </span>
                <span v-if="g.note" class="pcard-game-item__note" :class="noteTone(g.note.key)">
                  {{ noteText(g.note) }}
                </span>
              </div>
            </div>

            <svg class="pcard-game-item__arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </router-link>
        </div>
      </template>

      <!-- 动作回执卡片 -->
      <template v-else-if="c.kind === 'action'">
        <div class="pcard-receipt">
          <div class="pcard-receipt__dot"></div>
          <span class="pcard-receipt__text">{{ actionReceipt(c) }}</span>
          <router-link v-if="c.action === 'monitor_add'" class="pcard-receipt__link" to="/pool">
            {{ t('pilot.action.toFollows') }}
          </router-link>
        </div>
      </template>

      <!-- 导航卡片 -->
      <template v-else-if="c.kind === 'navigate' && (c as PilotNavigateFacts).path">
        <div class="pcard-receipt">
          <div class="pcard-receipt__dot"></div>
          <span class="pcard-receipt__text">{{ t('pilot.nav.done', { module: navModuleName((c as PilotNavigateFacts).target) }) }}</span>
        </div>
      </template>

      <!-- 系统诊断 / 代理通道 / 汇率多行卡片 -->
      <template v-else-if="c.kind === 'rows'">
        <div class="pcard-header-bar">
          <div class="pcard-header-bar__title">
            <svg class="pcard-header-bar__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="16" x2="12" y2="12" />
              <line x1="12" y1="8" x2="12.01" y2="8" />
            </svg>
            <span>{{ t(`pilot.rows.${c.titleKey}` as MessageKey) }}</span>
          </div>
          <span v-if="c.name" class="pcard-header-bar__subtitle">{{ c.name }}</span>
        </div>

        <div class="pcard-diagnostics-grid">
          <div v-for="(r, ri) in c.rows" :key="ri" class="pcard-diag-row">
            <div class="pcard-diag-row__head">
              <span class="pcard-diag-row__label">
                <template v-if="c.titleKey === 'rates'">
                  <CurrencyFlag :code="r.k" />
                  <span class="pcard-code-mono">({{ r.k }})</span>
                </template>
                <template v-else>
                  {{ rowLabel(r) }}
                </template>
              </span>
              <span v-if="r.at" class="pcard-diag-row__time">{{ dateOf(r.at) }}</span>
            </div>

            <div v-if="rowValue(r) || r.v" class="pcard-diag-row__status">
              <span class="pcard-status-pill" :class="r.tone ? `is-${r.tone}` : 'is-default'">
                <i v-if="r.tone" class="pcard-status-pill__dot" aria-hidden="true"></i>
                <template v-if="rowValue(r)">{{ rowValue(r) }}</template>
                <template v-if="rowValue(r) && r.v"> · </template>
                <template v-if="r.v">{{ r.v }}</template>
              </span>
            </div>
          </div>
        </div>
      </template>

      <!-- 家庭组卡片 -->
      <template v-else-if="c.kind === 'family'">
        <div class="pcard-header-bar">
          <div class="pcard-header-bar__title">
            <svg class="pcard-header-bar__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
              <circle cx="9" cy="7" r="4" />
              <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
              <path d="M16 3.13a4 4 0 0 1 0 7.75" />
            </svg>
            <span>{{ t('pilot.rows.family') }}</span>
          </div>
        </div>

        <template v-if="c.bound">
          <div class="pcard-fam-list">
            <div
              v-for="m in c.members"
              :key="m.steamid"
              class="pcard-fam-item"
              :class="m.role === 'primary' ? 'pcard-fam-item--primary' : 'pcard-fam-item--member'"
            >
              <div class="pcard-fam-item__avatar">
                <HlImg :src="m.avatar || undefined" :alt="familyMemberName(m)" loading="lazy">
                  <template #fallback>
                    <span>{{ (familyMemberName(m) || '?').slice(0, 1) }}</span>
                  </template>
                </HlImg>
              </div>

              <div class="pcard-fam-item__info">
                <div class="pcard-fam-item__top">
                  <span class="pcard-fam-item__name" :title="familyMemberName(m)">{{ familyMemberName(m) }}</span>
                  <span class="pcard-fam-item__role-badge" :class="m.role === 'primary' ? 'is-primary' : 'is-member'">
                    {{ t(m.role === 'primary' ? 'family.role.primary' : 'family.role.family') }}
                  </span>
                </div>
                <div class="pcard-fam-item__sub">
                  <span>{{ t('family.member.friendCode', { code: steamFriendCode(m.steamid) }) }}</span>
                </div>
              </div>

              <div class="pcard-fam-item__region">
                <RegionFlag v-if="m.region" :code="m.region" compact />
                <span v-else class="pcard-fam-item__region-unset">
                  {{ t('family.member.regionUnset') }}
                </span>
              </div>
            </div>
          </div>

          <div class="pcard-fam-footer">
            <span v-if="c.walletRegion" class="pcard-fam-footer__wallet">
              <RegionFlag :code="c.walletRegion" compact />
            </span>
            <span v-if="c.total != null && c.total > c.members.length" class="pcard-fam-footer__more">
              {{ t('pilot.facts.more', { n: c.total }) }}
            </span>
          </div>
        </template>
        <div v-else class="pcard-fam-unbound">{{ t('pilot.row.famUnbound') }}</div>
      </template>

      <!-- 奖杯与成就卡片 -->
      <template v-else-if="c.kind === 'achievements'">
        <div class="pcard-header-bar">
          <div class="pcard-header-bar__title">
            <svg class="pcard-header-bar__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="12" cy="8" r="7" />
              <polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88" />
            </svg>
            <span>{{ t('pilot.rows.achievements') }}</span>
          </div>
        </div>

        <template v-if="c.hasCredential">
          <div class="pcard-ach-kpi">
            <div class="pcard-ach-kpi__left">
              <span class="pcard-ach-kpi__plat">{{ t('pilot.card.achPlatinum', { n: c.platinum }) }}</span>
            </div>
            <div class="pcard-ach-kpi__right">
              <span class="pcard-ach-kpi__pct">{{ t('pilot.card.achRate', { rate: c.completionRate ?? 0 }) }}</span>
            </div>
          </div>

          <div class="pcard-ach-progress">
            <i class="pcard-ach-progress__fill" :style="{ width: `${Math.max(0, Math.min(100, c.completionRate ?? 0))}%` }"></i>
          </div>

          <div class="pcard-ach-stats-text">
            {{ t('pilot.row.achLine', { platinum: c.platinum, unlocked: c.unlocked, total: c.total }) }}
          </div>

          <div v-if="c.platinums.length" class="pcard-ach-shelf">
            <router-link
              v-for="p in c.platinums"
              :key="p.appid"
              class="pcard-ach-game-slot"
              :to="`/game/${p.appid}`"
              :title="p.name || `AppID ${p.appid}`"
            >
              <HlImg :src="pilotCoverUrl(p.appid)" :alt="p.name || `AppID ${p.appid}`" loading="lazy">
                <template #fallback>
                  <span>{{ (p.name || `AppID ${p.appid}`).slice(0, 2) }}</span>
                </template>
              </HlImg>
            </router-link>
          </div>

          <div v-if="c.recent.length" class="pcard-ach-recent">
            <div class="pcard-ach-recent__title">{{ t('pilot.card.recentUnlocks') }}</div>
            <div v-for="a in c.recent" :key="`${a.appid}-${a.name}`" class="pcard-ach-recent__item">
              <span class="pcard-ach-recent__name">{{ a.name }}</span>
              <span class="pcard-ach-recent__game">{{ a.gameName || `AppID ${a.appid}` }}</span>
              <span v-if="a.at" class="pcard-ach-recent__date">{{ dateOf(new Date((a.at as number) * 1000).toISOString()) }}</span>
            </div>
          </div>
        </template>
        <div v-else class="pcard-fam-unbound">{{ t('pilot.card.achNoCredential') }}</div>
      </template>

      <!-- 批量提议与高可信度决策面板 -->
      <template v-else-if="c.kind === 'proposal'">
        <div
          class="pcard-proposal"
          :tabindex="c.state === 'pending' ? 0 : undefined"
          @keydown="proposalKeydown(c, $event)"
          @compositionstart.capture="imeComposing = true"
          @compositionend.capture="imeComposing = false; imeEnded = true"
          @keyup.capture="imeEnded = false"
        >
          <div class="pcard-proposal__head">
            <div class="pcard-proposal__shield">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
            </div>
            <div class="pcard-proposal__titles">
              <span class="pcard-proposal__main-title">{{ proposalTitle(c) }}</span>
              <span v-if="c.state === 'pending'" class="pcard-proposal__desc">{{ t('pilot.proposal.hint') }}</span>
            </div>
          </div>

          <div v-if="c.action === 'delete'" class="pcard-del-list">
            <div v-for="it in c.items.slice(0, 8)" :key="it.key ?? it.appid" class="pcard-del-row">
              <HlImg
                v-if="it.appid"
                :src="pilotCoverUrl(it.appid)"
                :alt="it.name"
                loading="lazy"
                class="pcard-del-row__cover"
              >
                <template #fallback>
                  <span>{{ (it.name || `AppID ${it.appid}`).slice(0, 2) }}</span>
                </template>
              </HlImg>
              <div class="pcard-del-row__main">
                <span class="pcard-del-row__name">{{ it.name }}</span>
                <span v-if="it.reason" class="pcard-del-row__reason">{{ t(`pilot.del.reason.${it.reason}` as MessageKey) }}</span>
              </div>
            </div>
            <span v-if="c.items.length > 8" class="pcard-del-more-pill">+{{ c.items.length - 8 }}</span>
          </div>

          <div v-else class="pcard-item-chips">
            <span v-for="it in c.items.slice(0, 8)" :key="it.appid" class="pcard-item-chip">{{ it.name }}</span>
            <span v-if="c.items.length > 8" class="pcard-item-chip pcard-item-chip--more">+{{ c.items.length - 8 }}</span>
          </div>

          <div v-if="c.state === 'pending'" class="pcard-proposal__actions">
            <HlButton
              variant="primary"
              size="sm"
              :disabled="busyPid === c.pid"
              @click="emit('proposal', { pid: c.pid, approve: true })"
            >
              {{ t('pilot.proposal.confirm') }}
            </HlButton>
            <HlButton
              variant="text"
              size="sm"
              :disabled="busyPid === c.pid"
              @click="emit('proposal', { pid: c.pid, approve: false })"
            >
              {{ t('pilot.proposal.cancel') }}
            </HlButton>
          </div>

          <div v-else class="pcard-receipt">
            <div class="pcard-receipt__dot"></div>
            <span class="pcard-receipt__text">{{ proposalResult(c) }}</span>
          </div>
        </div>
      </template>

      <!-- 管道工作流卡片 -->
      <template v-else-if="c.kind === 'stepper'">
        <HlPilotStepper :card="c" />
      </template>
    </div>

    <!-- 超过2张卡片折叠条 -->
    <div v-if="visible.length > 2" class="pcards-more">
      <HlButton
        variant="text"
        size="sm"
        class="pcards-more__btn"
        @click="expanded = !expanded"
      >
        {{ expanded ? t('pilot.card.collapse') : t('pilot.card.expandMore', { n: visible.length - 2 }) }}
      </HlButton>
    </div>
  </div>
</template>

<style scoped>
.pcards-container {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  align-self: stretch;
}

.pcards-more {
  display: flex;
  justify-content: center;
  padding: 6px 0 2px;
}

.pcards-more__btn {
  font-size: 12px;
  color: var(--accent);
}

/* 统一基础卡片容器 */
.pcard {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  max-width: 100%;
  min-width: 0;
  overflow: hidden;
  box-sizing: border-box;
  box-shadow: 0 4px 16px var(--surface-inset-sm);
  transition: border-color var(--transition), box-shadow var(--transition);
}

.pcard:hover {
  border-color: var(--border-strong);
  box-shadow: 0 6px 20px var(--surface-inset);
}

/* ═════════ 价格卡 HERO 排版 ═════════ */
.pcard-hero {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.pcard-hero__cover {
  flex: none;
  width: 88px;
  aspect-ratio: 460 / 215;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
  border: 1px solid var(--border-soft);
  box-shadow: 0 2px 6px var(--surface-inset);
}

.pcard-hero__cover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.pcard-hero__cover-fallback {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
}

.pcard-hero__info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard-hero__title-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.pcard-hero__title {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: color var(--transition);
}

.pcard-hero__title:hover {
  color: var(--accent);
}

.pcard-hero__title-arrow {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
  opacity: 0.6;
}

.pcard-hero__badges {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

/* 统一胶囊徽标 */
.pcard-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.4;
}

.pcard-pill__dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.pcard-pill__spark {
  width: 5px;
  height: 5px;
  border-radius: 1px;
  background: currentColor;
  transform: rotate(45deg);
}

.pcard-pill--rating {
  background: var(--surface-chip);
  color: var(--text-secondary);
}

.pcard-pill--rating.good {
  color: var(--success);
  background: var(--success-a15);
}

.pcard-pill--rating.medium {
  color: var(--warning);
  background: var(--warning-a15);
}

.pcard-pill--rating.low {
  color: var(--danger);
  background: var(--danger-a15);
}

.pcard-pill--advice.is-success {
  background: var(--success-a15);
  color: var(--success);
}

.pcard-pill--advice.is-accent {
  background: var(--accent-a15);
  color: var(--accent);
}

.pcard-pill--advice.is-warning {
  background: var(--warning-a15);
  color: var(--warning);
}

.pcard-pill--advice.is-muted {
  background: var(--surface-chip);
  color: var(--text-muted);
}

/* 价格主看板 */
.pcard-price-board {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px 16px;
  padding: 8px 12px;
  background: var(--surface-panel);
  border-radius: var(--radius);
  border: 1px solid var(--border-soft);
}

.pcard-price-board__primary {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.pcard-price-board__now {
  display: flex;
  align-items: baseline;
  gap: 2px;
  color: var(--text-primary);
}

.pcard-price-board__currency {
  font-size: 15px;
  font-weight: 600;
  color: var(--accent);
}

.pcard-price-board__val {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.02em;
}

.pcard-price-board__alt {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.pcard-price-board__alt-val {
  font-weight: 600;
  color: var(--text-primary);
}

.pcard-disc-tag {
  display: inline-block;
  padding: 2px 7px;
  border-radius: var(--radius-sm);
  background: var(--success);
  color: var(--ink-on-fill);
  font-size: 12px;
  font-weight: 700;
  line-height: 1.2;
}

.pcard-disc-tag--sm {
  font-size: 11px;
  padding: 1px 5px;
}

.pcard-disc-tag--xs {
  font-size: 10px;
  padding: 1px 4px;
}

/* 价格基准指标微卡网格 */
.pcard-metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 8px;
}

.pcard-metric-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
}

.pcard-metric-card__k {
  font-size: 11px;
  color: var(--text-muted);
}

.pcard-metric-card__v {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.pcard-metric-card__v.is-highlight {
  color: var(--success);
}

.pcard-metric-card__sub {
  font-size: 10px;
  color: var(--text-faint);
}

/* 可视化区间水位标尺 */
.pcard-gauge {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 2px;
}

.pcard-gauge__header {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-faint);
}

.pcard-gauge__track {
  position: relative;
  height: 6px;
  border-radius: 999px;
  background: var(--surface-track);
}

.pcard-gauge__tick {
  position: absolute;
  top: -2px;
  width: 2px;
  height: 10px;
  border-radius: 1px;
  background: var(--text-faint);
}

.pcard-gauge__dot {
  position: absolute;
  top: 50%;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--bg-card);
  transform: translate(-50%, -50%);
  box-shadow: 0 0 0 3px var(--accent-a20);
}

/* 折叠走势图 */
.pcard-trend-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 4px;
}

.pcard-trend-toggle {
  display: inline-flex;
  align-items: center;
  align-self: flex-start;
  gap: 6px;
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-soft);
  background: var(--surface-chip);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition);
}

.pcard-trend-toggle:hover {
  background: var(--accent-a10);
  border-color: var(--accent-a30);
  color: var(--accent);
}

.pcard-trend-toggle__icon {
  width: 13px;
  height: 13px;
}

.pcard-trend-toggle__chevron {
  width: 12px;
  height: 12px;
  transition: transform var(--transition);
}

.pcard-trend-toggle__chevron.is-open {
  transform: rotate(180deg);
}

.pcard-trend-content {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--radius);
  background: var(--surface-panel);
  border: 1px solid var(--border-soft);
}

.pcard-trend-footnote {
  font-size: 11px;
  color: var(--text-muted);
}

/* ═════════ 游戏清单卡 ═════════ */
.pcard-header-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.pcard-header-bar__title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  min-width: 0;
}

.pcard-header-bar__icon {
  width: 15px;
  height: 15px;
  color: var(--accent);
  flex-shrink: 0;
}

.pcard-header-bar__badge {
  font-size: 11px;
  color: var(--text-muted);
}

.pcard-header-bar__subtitle {
  font-size: 12px;
  color: var(--text-muted);
}

.pcard-filter-pills {
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: none;
  padding-bottom: 2px;
}

.pcard-filter-pill {
  appearance: none;
  border: 1px solid var(--border-soft);
  background: var(--surface-chip);
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 3px 10px;
  font-size: 11px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  transition: all var(--transition);
}

.pcard-filter-pill:hover {
  border-color: var(--accent-a40);
  color: var(--text-primary);
}

.pcard-filter-pill.is-active {
  background: var(--accent-a15);
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}

.pcard-filter-pill__count {
  font-size: 10px;
  opacity: 0.8;
  background: var(--bg-card);
  padding: 0 4px;
  border-radius: 999px;
}

.pcard-empty-state {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px;
  border-radius: var(--radius-sm);
  background: var(--surface-panel);
  border: 1px dashed var(--border-soft);
  font-size: 12px;
  color: var(--text-muted);
}

.pcard-empty-state__reset {
  appearance: none;
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font-size: 11px;
  text-decoration: underline;
}

.pcard-game-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard-game-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius);
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
  text-decoration: none;
  color: inherit;
  transition: all var(--transition);
}

.pcard-game-item:hover {
  background: var(--accent-a08);
  border-color: var(--accent-a30);
  transform: translateX(2px);
}

.pcard-game-item__index {
  flex: none;
  width: 16px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-align: center;
}

.pcard-game-item__cover {
  flex: none;
  width: 68px;
  aspect-ratio: 460 / 215;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
}

.pcard-game-item__cover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.pcard-game-item__fallback {
  font-size: 10px;
  color: var(--text-muted);
}

.pcard-game-item__content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.pcard-game-item__top {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.pcard-game-item__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pcard-cn-tag {
  font-size: 10px;
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  background: var(--accent-a10);
  color: var(--accent);
  line-height: 1.2;
  flex-shrink: 0;
}

.pcard-game-item__bottom {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}

.pcard-game-item__rating {
  font-weight: 500;
  color: var(--text-secondary);
}

.pcard-game-item__rating.good {
  color: var(--success);
}

.pcard-game-item__rating.medium {
  color: var(--warning);
}

.pcard-game-item__rating.low {
  color: var(--danger);
}

.pcard-game-item__price-block {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.pcard-game-item__price {
  font-weight: 600;
  color: var(--text-primary);
}

.pcard-game-item__note {
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 180px;
}

.pcard-game-item__note.is-ok {
  color: var(--success);
}

.pcard-game-item__arrow {
  flex: none;
  width: 14px;
  height: 14px;
  color: var(--text-muted);
  opacity: 0.5;
  transition: transform var(--transition);
}

.pcard-game-item:hover .pcard-game-item__arrow {
  opacity: 1;
  color: var(--accent);
  transform: translateX(2px);
}

/* ═════════ 动作与导航回执 ═════════ */
.pcard-receipt {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--surface-panel);
  border: 1px solid var(--border-soft);
  font-size: 13px;
  color: var(--text-primary);
}

.pcard-receipt__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--success);
  box-shadow: 0 0 0 3px var(--success-a20);
  flex-shrink: 0;
}

.pcard-receipt__text {
  flex: 1;
  min-width: 0;
}

.pcard-receipt__link {
  font-size: 12px;
  color: var(--accent);
  text-decoration: none;
  font-weight: 500;
}

.pcard-receipt__link:hover {
  text-decoration: underline;
}

/* ═════════ 诊断与多行数据看板 ═════════ */
.pcard-diagnostics-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard-diag-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
}

.pcard-diag-row__head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.pcard-diag-row__label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
}

.pcard-code-mono {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
}

.pcard-diag-row__time {
  font-size: 10px;
  color: var(--text-faint);
}

.pcard-diag-row__status {
  flex: none;
}

.pcard-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  background: var(--surface-panel);
  color: var(--text-secondary);
}

.pcard-status-pill__dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.pcard-status-pill.is-ok {
  background: var(--success-a15);
  color: var(--success);
}

.pcard-status-pill.is-warn {
  background: var(--warning-a15);
  color: var(--warning);
}

.pcard-status-pill.is-bad {
  background: var(--danger-a15);
  color: var(--danger);
}

/* ═════════ 家庭组卡片 ═════════ */
.pcard-fam-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard-fam-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
  border-left: 3px solid transparent;
}

.pcard-fam-item--primary {
  border-left-color: var(--accent);
}

.pcard-fam-item--member {
  border-left-color: var(--purple);
}

.pcard-fam-item__avatar {
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-panel);
  display: grid;
  place-items: center;
  font-size: 13px;
  font-weight: 700;
  color: var(--text-primary);
}

.pcard-fam-item__avatar :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.pcard-fam-item__info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pcard-fam-item__top {
  display: flex;
  align-items: center;
  gap: 6px;
}

.pcard-fam-item__name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pcard-fam-item__role-badge {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: var(--radius-sm);
  font-weight: 600;
}

.pcard-fam-item__role-badge.is-primary {
  background: var(--accent-a15);
  color: var(--accent);
}

.pcard-fam-item__role-badge.is-member {
  background: var(--purple-a15);
  color: var(--purple);
}

.pcard-fam-item__sub {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.pcard-fam-item__region-unset {
  font-size: 11px;
  color: var(--text-faint);
}

.pcard-fam-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-muted);
}

.pcard-fam-unbound {
  font-size: 12px;
  color: var(--warning);
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--warning-a15);
}

/* ═════════ 奖杯卡片 ═════════ */
.pcard-ach-kpi {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}

.pcard-ach-kpi__plat {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
}

.pcard-ach-kpi__pct {
  font-size: 13px;
  font-weight: 600;
  color: var(--accent);
}

.pcard-ach-progress {
  height: 6px;
  border-radius: 999px;
  background: var(--surface-track);
  overflow: hidden;
}

.pcard-ach-progress__fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--accent);
}

.pcard-ach-stats-text {
  font-size: 12px;
  color: var(--text-secondary);
}

.pcard-ach-shelf {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding-bottom: 4px;
}

.pcard-ach-game-slot {
  flex: none;
  width: 90px;
  aspect-ratio: 460 / 215;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-panel);
  border: 1px solid var(--border-soft);
  display: grid;
  place-items: center;
}

.pcard-ach-game-slot :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.pcard-ach-recent {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding-top: 4px;
}

.pcard-ach-recent__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
}

.pcard-ach-recent__item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
}

.pcard-ach-recent__name {
  font-weight: 500;
  color: var(--text-primary);
}

.pcard-ach-recent__game {
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pcard-ach-recent__date {
  color: var(--text-faint);
  flex-shrink: 0;
}

/* ═════════ 提议与确认面板 ═════════ */
.pcard-proposal {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.pcard-proposal__head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.pcard-proposal__shield {
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-sm);
  background: var(--warning-a15);
  color: var(--warning);
  display: grid;
  place-items: center;
}

.pcard-proposal__shield svg {
  width: 18px;
  height: 18px;
}

.pcard-proposal__titles {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pcard-proposal__main-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.pcard-proposal__desc {
  font-size: 12px;
  color: var(--text-muted);
}

.pcard-del-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard-del-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  background: var(--surface-panel);
  border: 1px solid var(--border-soft);
}

.pcard-del-row__cover {
  flex: none;
  width: 48px;
  aspect-ratio: 460 / 215;
  border-radius: 4px;
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
}

.pcard-del-row__cover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.pcard-del-row__main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.pcard-del-row__name {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pcard-del-row__reason {
  font-size: 11px;
  color: var(--warning);
  flex-shrink: 0;
}

.pcard-del-more-pill {
  font-size: 11px;
  color: var(--text-muted);
  padding: 2px 6px;
}

.pcard-item-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.pcard-item-chip {
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
  font-size: 12px;
  color: var(--text-primary);
}

.pcard-item-chip--more {
  color: var(--text-muted);
}

.pcard-proposal__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 4px;
}
</style>
