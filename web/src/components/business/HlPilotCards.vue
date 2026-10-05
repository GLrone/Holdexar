<script setup lang="ts">
/**
 * 领航台结构化卡片：agent 工具结果的富渲染层（数据层 = client.PilotFacts）。
 * 四种卡：price 价格卡（现价/折扣/史低/近一年区间条）、games 候选列表卡
 * （整行可点进详情）、action 动作回执卡、navigate 导航卡。
 * 样式只取设计 token；折扣徽章/好评率分级沿用游戏卡既有惯例。
 */
import { computed, ref } from 'vue'

import {
  formatCnyFen,
} from '@/api/regions'
import type {
  PilotAchievementFacts,
  PilotActionFacts,
  PilotFacts,
  PilotFamilyFacts,
  PilotGameFactsItem,
  PilotNavigateFacts,
  PilotPriceFacts,
  PilotProposalFacts,
  PilotRowsFacts,
  PilotStepperFacts,
} from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import { pilotCoverUrl, positivePct, ratingClass } from '@/lib/pilotView'
import HlImg from '@/components/ui/HlImg.vue'
import CurrencyFlag from '@/components/CurrencyFlag.vue'
import RegionFlag from '@/components/RegionFlag.vue'
import HlPilotCompare from '@/components/business/HlPilotCompare.vue'
import HlPilotRegions from '@/components/business/HlPilotRegions.vue'
import HlPilotStepper from '@/components/business/HlPilotStepper.vue'
import HlPilotTrend from '@/components/business/HlPilotTrend.vue'

const props = defineProps<{
  cards: PilotFacts[]
  /** 正在提交确认的提议 pid（父组件持有请求态，卡片只负责禁用按钮） */
  busyPid?: string | null
}>()

const emit = defineEmits<{
  (e: 'proposal', payload: { pid: string; approve: boolean }): void
}>()

const { t, locale } = useI18n()

const visible = computed(() => (props.cards || []).filter(Boolean))

function fen(v: number | null | undefined): string {
  return typeof v === 'number' && v > 0 ? formatCnyFen(v) : '—'
}

function ratingText(rate: number | null | undefined): string | null {
  const pct = positivePct(rate)
  return pct === null ? null : `${pct}%`
}

/* 量级缩写（12.3 万条评测）——按语言缓存的 Intl 格式化器 */
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

/* 区间条定位：现价在近一年 min–max 中的位置（0–100），退化区间不出条 */
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

/* 提议卡键盘流守卫态：composition 跟踪（IME 确认的 Enter 不是决策） */
const imeComposing = ref(false)
const imeEnded = ref(false)

function proposalKeydown(c: PilotProposalFacts, e: KeyboardEvent) {
  if (c.state !== 'pending' || props.busyPid === c.pid) return
  const el = e.target as Element
  if (e.defaultPrevented || !e.currentTarget.contains(document.activeElement)) return
  if (el.closest('input, textarea, select, [contenteditable="true"], [contenteditable=""]')) return
  if (e.key !== 'Enter' && e.key !== 'Escape') return
  // 焦点在按钮上时 Enter 走按钮原生激活，不在此二义触发
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
    // 格式异常或无效 sid 兜底
  }
  return sid ? sid.slice(-6) : '—'
}

function familyMemberName(m: { steamid: string; name?: string | null }): string {
  return m.name || t('family.member.unnamed', { id: m.steamid.slice(-4) })
}

/** 行键：kindKey 走词条，否则用后端原文。 */
function rowLabel(r: RowsRow): string {
  return r.kindKey ? t(r.kindKey as MessageKey) : r.k
}

/** 行值：vKey 词条渲染，data 里 *Fen 键先格式化为 ¥（priceFen→price）再插值；无 vKey 返回空。 */
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

/** 清单卡行内注记：同 rows 行值一套词条（pilot.row.*），v 原样值、at 截日期。 */
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
  <div
    v-for="(c, ci) in visible"
    :key="ci"
    class="pcard"
    :class="`pcard--${c.kind}`"
  >
    <!-- 价格卡：现价/折扣/史低 + 近一年区间条 + 好评口碑 -->
    <template v-if="c.kind === 'price'">
      <div class="pcard__head">
        <HlImg :src="pilotCoverUrl(c.appid)" :alt="c.name || `AppID ${c.appid}`" loading="lazy" class="pcard__cover">
          <template #fallback>
            <span class="pcard__cover-fallback">{{ (c.name || `AppID ${c.appid}`).slice(0, 2) }}</span>
          </template>
        </HlImg>
        <div class="pcard__head-info">
          <router-link class="pcard__name" :to="`/game/${c.appid}`" :title="c.name || `AppID ${c.appid}`">
            {{ c.name || `AppID ${c.appid}` }}
          </router-link>
          <span
            v-if="ratingText(c.positiveRate)"
            class="pcard__rating"
            :class="ratingClass(c.positiveRate)"
          >
            {{ t('pilot.card.rating', { rate: ratingText(c.positiveRate) }) }}
            <template v-if="c.reviewCount"> · {{ fmtCount(c.reviewCount) }}</template>
          </span>
        </div>
      </div>

      <div class="pcard__main">
        <div class="pcard__now">
          <span class="pcard__price">{{ fen(c.cn?.cnyFen) }}</span>
          <span v-if="c.cn?.discount" class="pcard__disc">-{{ c.cn.discount }}%</span>
          <span
            v-if="getDealAdvice(c)"
            class="pcard__advice"
            :class="`is-${getDealAdvice(c)!.tone}`"
          >
            <i class="pcard__advice-dot" aria-hidden="true"></i>
            {{ t(getDealAdvice(c)!.labelKey) }}
          </span>
        </div>
        <div v-if="c.cn?.cnyFen == null && c.alt" class="pcard__atl">
          <span class="pcard__atl-label">{{ t('pilot.card.altRegion', { region: c.alt.region }) }}</span>
          <span class="pcard__atl-price">{{ fen(c.alt.cnyFen) }}</span>
          <span v-if="c.alt.discount" class="pcard__disc">-{{ c.alt.discount }}%</span>
        </div>
        <div class="pcard__atl">
          <span class="pcard__atl-label">{{ t('pilot.card.atl') }}</span>
          <span class="pcard__atl-price">{{ fen(c.lowest?.cnyFen) }}</span>
          <span v-if="dateOf(c.lowest?.snapshotAt)" class="pcard__atl-date">{{ dateOf(c.lowest?.snapshotAt) }}</span>
        </div>
      </div>

      <div v-if="rangePos(c) !== null" class="pcard__range">
        <span class="pcard__range-min">{{ fen(c.year?.minFen) }}</span>
        <div class="pcard__track">
          <i
            class="pcard__tick"
            :style="{ left: `${medianPos(c)}%` }"
            :title="t('pilot.card.medianTick')"
          ></i>
          <i class="pcard__dot" :style="{ left: `${rangePos(c)}%` }"></i>
        </div>
        <span class="pcard__range-max">{{ fen(c.year?.maxFen) }}</span>
      </div>

      <div v-if="(c.trend && c.trend.length >= 2) || c.year" class="pcard__expandable">
        <button
          type="button"
          class="pcard__toggle-trend"
          :aria-expanded="isTrendExpanded(c.appid)"
          @click="toggleTrend(c.appid)"
        >
          <span>{{ t(isTrendExpanded(c.appid) ? 'pilot.card.hideTrend' : 'pilot.card.toggleTrend') }}</span>
          <svg class="pcard__toggle-chevron" :class="{ 'is-open': isTrendExpanded(c.appid) }" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <div v-if="isTrendExpanded(c.appid)" class="pcard__trend-box">
          <HlPilotTrend v-if="c.trend && c.trend.length >= 2" :points="c.trend" />
          <div v-if="c.year" class="pcard__foot">
            {{ t('pilot.card.year', { min: fen(c.year.minFen), max: fen(c.year.maxFen), median: fen(c.year.medianFen) }) }}
            <template v-if="c.year.count"> · {{ t('pilot.card.obs', { count: c.year.count }) }}</template>
          </div>
        </div>
      </div>
    </template>

    <HlPilotRegions v-else-if="c.kind === 'regions'" :card="c" />

    <HlPilotCompare v-else-if="c.kind === 'compare'" :card="c" />

    <!-- 游戏清单卡：封面+价格+折扣+好评（找游戏同款）；titleKey 区分榜单/关注/愿望单等来源 -->
    <template v-else-if="c.kind === 'games'">
      <div class="pcard__title">
        {{ c.titleKey ? t(`pilot.rows.${c.titleKey}` as MessageKey) : t('pilot.facts.gamesTitle') }}
        <span v-if="c.total != null && c.total > c.items.length" class="pcard__title-more">
          {{ t('pilot.facts.more', { n: c.total }) }}
        </span>
      </div>

      <!-- 内嵌式交互过滤胶囊：本地即时微调与二次筛选 -->
      <div v-if="getFilterChips(c).length > 1" class="pcard__filters">
        <button
          v-for="chip in getFilterChips(c)"
          :key="chip.key"
          type="button"
          class="pcard__filter-chip"
          :class="{ 'is-active': getActiveFilter(ci) === chip.key }"
          @click="setGameFilter(ci, chip.key)"
        >
          <span>{{ t(chip.labelKey) }}</span>
          <span class="pcard__filter-count">{{ chip.count }}</span>
        </button>
      </div>

      <div v-if="filteredGameItems(c, ci).length === 0" class="pcard__filter-empty">
        <span class="pcard__filter-empty-text">{{ t('pilot.filter.empty') }}</span>
        <button type="button" class="pcard__filter-reset-btn" @click="setGameFilter(ci, 'all')">
          {{ t('pilot.filter.reset') }}
        </button>
      </div>

      <div v-else class="pcard__gamelist">
        <router-link
          v-for="(g, gi) in filteredGameItems(c, ci)"
          :key="g.appid"
          class="pcard__row"
          :to="`/game/${g.appid}`"
        >
          <span class="pcard__idx">{{ gi + 1 }}</span>
          <HlImg :src="pilotCoverUrl(g.appid)" :alt="g.name || `AppID ${g.appid}`" loading="lazy" class="pcard__cover">
            <template #fallback>
              <span class="pcard__cover-fallback">{{ (g.name || `AppID ${g.appid}`).slice(0, 2) }}</span>
            </template>
          </HlImg>
          <div class="pcard__row-main">
            <div class="pcard__row-name-line">
              <span class="pcard__row-name" :title="g.name || `AppID ${g.appid}`">{{ g.name || `AppID ${g.appid}` }}</span>
              <span
                v-if="hasSimplifiedChinese(g.chineseSupport)"
                class="pcard__cn-badge"
              >
                {{ t('pilot.filter.chinese') }}
              </span>
            </div>
            <div class="pcard__row-sub">
              <span v-if="ratingText(g.positiveRate)" class="pcard__row-rating" :class="ratingClass(g.positiveRate)">
                {{ ratingText(g.positiveRate) }}
              </span>
              <span class="pcard__row-price">
                <span class="pcard__row-fen">{{ fen(g.cnyFen) }}</span>
                <span v-if="g.discount" class="pcard__row-disc">-{{ g.discount }}%</span>
              </span>
              <span v-if="g.note" class="pcard__row-note" :class="noteTone(g.note.key)">
                {{ noteText(g.note) }}
              </span>
            </div>
          </div>
        </router-link>
      </div>
    </template>

    <!-- 动作回执卡 -->
    <template v-else-if="c.kind === 'action'">
      <div class="pcard__done">
        <i class="pcard__done-dot" aria-hidden="true"></i>
        <span class="pcard__done-text">{{ actionReceipt(c) }}</span>
        <router-link v-if="c.action === 'monitor_add'" class="pcard__link" to="/pool">
          {{ t('pilot.action.toFollows') }}
        </router-link>
      </div>
    </template>

    <!-- 导航卡 -->
    <template v-else-if="c.kind === 'navigate' && (c as PilotNavigateFacts).path">
      <div class="pcard__done">
        <i class="pcard__done-dot" aria-hidden="true"></i>
        <span class="pcard__done-text">{{ t('pilot.nav.done', { module: navModuleName((c as PilotNavigateFacts).target) }) }}</span>
      </div>
    </template>

    <!-- 行卡：清单与诊断（关注/提醒/捆绑包/价格诊断/失败任务/代理通道/写调度/汇率） -->
    <template v-else-if="c.kind === 'rows'">
      <div class="pcard__title">
        {{ t(`pilot.rows.${c.titleKey}` as MessageKey) }}
        <span v-if="c.name" class="pcard__title-more">· {{ c.name }}</span>
      </div>
      <div class="pcard__records">
        <div v-for="(r, ri) in c.rows" :key="ri" class="pcard__record">
          <div class="pcard__record-head">
            <span class="pcard__record-k">
              <template v-if="c.titleKey === 'rates'">
                <CurrencyFlag :code="r.k" />
                <span class="pcard__code-badge">({{ r.k }})</span>
              </template>
              <template v-else>
                {{ rowLabel(r) }}
              </template>
            </span>
            <span v-if="r.at" class="pcard__record-at">{{ dateOf(r.at) }}</span>
          </div>
          <div v-if="rowValue(r) || r.v" class="pcard__record-body">
            <span
              class="pcard__badge"
              :class="r.tone ? `is-${r.tone}` : 'is-default'"
            >
              <i v-if="r.tone" class="pcard__badge-dot" aria-hidden="true"></i>
              <template v-if="rowValue(r)">{{ rowValue(r) }}</template>
              <template v-if="rowValue(r) && r.v"> · </template>
              <template v-if="r.v">{{ r.v }}</template>
            </span>
          </div>
        </div>
      </div>
    </template>

    <!-- 家庭组卡：成员分行复用家庭页 fam-row 设计语言 -->
    <template v-else-if="c.kind === 'family'">
      <div class="pcard__title">{{ t('pilot.rows.family') }}</div>
      <template v-if="c.bound">
        <div class="pcard__fam-list">
          <div
            v-for="m in c.members"
            :key="m.steamid"
            class="pcard__fam-row"
            :class="m.role === 'primary' ? 'pcard__fam-row--primary' : 'pcard__fam-row--family'"
          >
            <div
              class="pcard__fam-ava"
              :style="{
                background: m.role === 'primary'
                  ? 'linear-gradient(135deg, var(--accent-fill), var(--accent))'
                  : 'linear-gradient(135deg, var(--purple), var(--purple-deep))',
              }"
            >
              <HlImg :src="m.avatar || undefined" :alt="familyMemberName(m)" loading="lazy">
                <template #fallback>
                  <span>{{ (familyMemberName(m) || '?').slice(0, 1) }}</span>
                </template>
              </HlImg>
            </div>
            <div class="pcard__fam-main">
              <div class="pcard__fam-name-line">
                <span class="pcard__fam-name" :title="familyMemberName(m)">{{ familyMemberName(m) }}</span>
                <span
                  class="pcard__fam-role"
                  :class="m.role === 'primary' ? 'pcard__fam-role--primary' : 'pcard__fam-role--family'"
                >
                  {{ t(m.role === 'primary' ? 'family.role.primary' : 'family.role.family') }}
                </span>
              </div>
              <div class="pcard__fam-sub-line">
                <span class="pcard__fam-code">{{ t('family.member.friendCode', { code: steamFriendCode(m.steamid) }) }}</span>
              </div>
            </div>
            <div class="pcard__fam-side">
              <RegionFlag v-if="m.region" :code="m.region" compact />
              <span v-else class="pcard__fam-region is-unset">
                {{ t('family.member.regionUnset') }}
              </span>
            </div>
          </div>
        </div>
        <div class="pcard__foot">
          <template v-if="c.walletRegion">
            <span class="pcard__fam-wallet">
              <RegionFlag :code="c.walletRegion" compact />
            </span>
          </template>
          <template v-if="c.total != null && c.total > c.members.length">
            · {{ t('pilot.facts.more', { n: c.total }) }}
          </template>
        </div>
      </template>
      <div v-else class="pcard__line-v is-warn">{{ t('pilot.row.famUnbound') }}</div>
    </template>

    <!-- 奖杯卡：KPI + 进度条 + 白金陈列 + 最近解锁（成就域 summary 投影） -->
    <template v-else-if="c.kind === 'achievements'">
      <div class="pcard__title">{{ t('pilot.rows.achievements') }}</div>
      <template v-if="c.hasCredential">
        <div class="pcard__ach-kpi">
          <span class="pcard__ach-plat">{{ t('pilot.card.achPlatinum', { n: c.platinum }) }}</span>
          <span class="pcard__ach-rate">{{ t('pilot.card.achRate', { rate: c.completionRate ?? 0 }) }}</span>
        </div>
        <div class="pcard__ach-track">
          <i :style="{ width: `${Math.max(0, Math.min(100, c.completionRate ?? 0))}%` }"></i>
        </div>
        <div class="pcard__ach-line">
          {{ t('pilot.row.achLine', { platinum: c.platinum, unlocked: c.unlocked, total: c.total }) }}
        </div>
        <div v-if="c.platinums.length" class="pcard__ach-plats">
          <router-link
            v-for="p in c.platinums"
            :key="p.appid"
            class="pcard__ach-plat-game"
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
        <div v-if="c.recent.length" class="pcard__ach-recent">
          <div class="pcard__ach-line">{{ t('pilot.card.recentUnlocks') }}</div>
          <div v-for="a in c.recent" :key="`${a.appid}-${a.name}`" class="pcard__line">
            <span class="pcard__line-k">{{ a.name }}</span>
            <span class="pcard__line-v">{{ a.gameName || `AppID ${a.appid}` }}</span>
            <span v-if="a.at" class="pcard__line-at">{{ dateOf(new Date((a.at as number) * 1000).toISOString()) }}</span>
          </div>
        </div>
      </template>
      <div v-else class="pcard__line-v is-warn">{{ t('pilot.card.achNoCredential') }}</div>
    </template>

    <!-- 批量提议卡：清单 + 确认/取消（确认前无任何写动作发生） -->
    <template v-else-if="c.kind === 'proposal'">
      <div
        class="pcard__focusable"
        :tabindex="c.state === 'pending' ? 0 : undefined"
        @keydown="proposalKeydown(c, $event)"
        @compositionstart.capture="imeComposing = true"
        @compositionend.capture="imeComposing = false; imeEnded = true"
        @keyup.capture="imeEnded = false"
      >
      <div class="pcard__title">{{ proposalTitle(c) }}</div>
      <p v-if="c.state === 'pending'" class="pcard__hint">{{ t('pilot.proposal.hint') }}</p>
      <!-- 删除提议：逐项解释（agent 只检查+解释，确认与执行权在用户） -->
      <div v-if="c.action === 'delete'" class="pcard__dellist">
        <div v-for="it in c.items.slice(0, 8)" :key="it.key ?? it.appid" class="pcard__delrow">
          <HlImg
            v-if="it.appid"
            :src="pilotCoverUrl(it.appid)"
            :alt="it.name"
            loading="lazy"
            class="pcard__delcover"
          >
            <template #fallback>
              <span>{{ (it.name || `AppID ${it.appid}`).slice(0, 2) }}</span>
            </template>
          </HlImg>
          <span class="pcard__delmain">
            <span class="pcard__delname">{{ it.name }}</span>
            <span class="pcard__delreason">{{ it.reason ? t(`pilot.del.reason.${it.reason}` as MessageKey) : '' }}</span>
          </span>
        </div>
        <span v-if="c.items.length > 8" class="pcard__chip pcard__chip--more">
          +{{ c.items.length - 8 }}
        </span>
      </div>
      <div v-else class="pcard__chips">
        <span v-for="it in c.items.slice(0, 8)" :key="it.appid" class="pcard__chip">{{ it.name }}</span>
        <span v-if="c.items.length > 8" class="pcard__chip pcard__chip--more">
          +{{ c.items.length - 8 }}
        </span>
      </div>
      <div v-if="c.state === 'pending'" class="pcard__acts">
        <button
          type="button"
          class="hl-btn hl-btn--primary"
          :disabled="busyPid === c.pid"
          @click="emit('proposal', { pid: c.pid, approve: true })"
        >
          {{ t('pilot.proposal.confirm') }}
        </button>
        <button
          type="button"
          class="hl-btn"
          :disabled="busyPid === c.pid"
          @click="emit('proposal', { pid: c.pid, approve: false })"
        >
          {{ t('pilot.proposal.cancel') }}
        </button>
      </div>
      <div v-else class="pcard__done">
        <i class="pcard__done-dot" aria-hidden="true"></i>
        <span class="pcard__done-text">{{ proposalResult(c) }}</span>
      </div>
      </div>
    </template>

    <!-- 工作流分步进度条卡片 -->
    <template v-else-if="c.kind === 'stepper'">
      <HlPilotStepper :card="c" />
    </template>
  </div>
</template>

<style scoped>
.pcard {
  display: grid;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  background: var(--bg-card);
  max-width: 100%;
  min-width: 0;
  overflow: hidden;
  box-sizing: border-box;
}

.pcard__head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.pcard__head-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* 封面缩略图：Steam header 460:215 原比例，404 落首字符占位（游戏卡同款兜底） */
.pcard__cover {
  flex: none;
  width: 64px;
  aspect-ratio: 460 / 215;
  border-radius: 5px;
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
}

.pcard__row .pcard__cover {
  width: 58px;
}

.pcard__cover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.pcard__cover-fallback {
  font-size: 11px;
  color: var(--text-secondary);
}

.pcard__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.pcard__name:hover {
  color: var(--accent);
}

.pcard__rating {
  font-size: 11px;
  color: var(--success);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pcard__rating.medium {
  color: var(--warning);
}

.pcard__rating.low {
  color: var(--text-secondary);
}

.pcard__main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
  min-width: 0;
}

.pcard__now {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  min-width: 0;
}

.pcard__price {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
}

.pcard__disc {
  padding: 2px 7px;
  border-radius: 5px;
  background: linear-gradient(135deg, var(--success), var(--success-deep));
  color: var(--ink-on-fill);
  font-size: 12px;
  font-weight: 700;
}

.pcard__advice {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.4;
}

.pcard__advice-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.pcard__advice.is-success {
  background: var(--success-a15);
  color: var(--success);
}

.pcard__advice.is-accent {
  background: var(--accent-a15);
  color: var(--accent);
}

.pcard__advice.is-warning {
  background: var(--warning-a15);
  color: var(--warning);
}

.pcard__advice.is-muted {
  background: var(--surface-inset);
  color: var(--text-secondary);
}

.pcard__expandable {
  margin-top: 4px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard__toggle-trend {
  display: inline-flex;
  align-items: center;
  align-self: flex-start;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid var(--border-soft);
  background: var(--surface-inset);
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.15s ease;
}

.pcard__toggle-trend:hover {
  color: var(--text-primary);
  border-color: var(--accent-a30);
  background: var(--accent-a10);
}

.pcard__toggle-chevron {
  width: 12px;
  height: 12px;
  transition: transform 0.2s ease;
}

.pcard__toggle-chevron.is-open {
  transform: rotate(180deg);
}

.pcard__trend-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 4px;
}

.pcard__atl {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.pcard__atl-price {
  font-weight: 600;
  color: var(--text-primary);
}

.pcard__atl-date {
  font-size: 11px;
}

.pcard__range {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-secondary);
}

.pcard__track {
  position: relative;
  flex: 1;
  height: 4px;
  border-radius: 2px;
  background: var(--border-soft);
}

.pcard__tick {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 10px;
  border-radius: 1px;
  background: var(--text-secondary);
  opacity: 0.55;
}

.pcard__dot {
  position: absolute;
  top: 50%;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent);
  border: 2px solid var(--bg-card);
  transform: translate(-50%, -50%);
  box-shadow: 0 0 0 2px var(--accent-a15);
}

.pcard__foot {
  font-size: 11px;
  color: var(--text-secondary);
}

.pcard__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.pcard__filters {
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
  scrollbar-width: none;
  padding: 2px 0 6px;
}

.pcard__filter-chip {
  appearance: none;
  border: 1px solid var(--border-soft);
  background: var(--bg-surface);
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 2px 9px;
  font-size: 11px;
  line-height: 1.4;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
  transition: all 0.15s ease;
}

.pcard__filter-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.pcard__filter-chip.is-active {
  background: var(--accent-a15);
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 500;
}

.pcard__filter-count {
  font-size: 10px;
  opacity: 0.8;
  background: var(--bg-card);
  padding: 0 4px;
  border-radius: 999px;
}

.pcard__filter-chip.is-active .pcard__filter-count {
  background: var(--accent-a15);
}

.pcard__filter-empty {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--bg-surface);
  border: 1px dashed var(--border-soft);
  font-size: 12px;
  color: var(--text-muted);
}

.pcard__filter-reset-btn {
  appearance: none;
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font-size: 11px;
  padding: 2px 6px;
  text-decoration: underline;
}

.pcard__row-name-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.pcard__cn-badge {
  font-size: 10px;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--accent-a10);
  color: var(--accent);
  border: 1px solid var(--accent-a20, var(--accent-a10));
  line-height: 1.3;
  flex-shrink: 0;
}

.pcard__gamelist {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.pcard__row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 8px;
  background: var(--surface-inset);
  border: 1px solid transparent;
  min-width: 0;
  transition: all 0.15s ease;
}

.pcard__row:hover {
  background: var(--accent-a10);
  border-color: var(--accent-a30);
}

.pcard__idx {
  flex-shrink: 0;
  width: 14px;
  font-size: 11px;
  color: var(--text-secondary);
  text-align: right;
}

.pcard__row-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.pcard__row-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.pcard__row-sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 11px;
}

.pcard__row-price {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text-primary);
  font-weight: 600;
}

.pcard__row-disc {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--success);
  color: var(--ink-on-fill);
  font-size: 10px;
  font-weight: 700;
}

.pcard__row-note {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--text-secondary);
}

.pcard__row-note.is-ok {
  color: var(--success);
}

.pcard__title-more {
  margin-left: 6px;
  font-size: 11px;
  font-weight: 400;
  color: var(--text-secondary);
}

/* 汇率国旗图标 */
.pcard__code-badge {
  margin-left: 4px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
}

/* 家庭组卡：成员列表（对齐家庭页 fam-row 布局） */
.pcard__fam-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard__fam-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--surface-overlay);
  border: 1px solid var(--border-subtle);
  border-left: 3px solid transparent;
  transition: background 0.15s ease;
}

.pcard__fam-row--primary {
  border-left-color: var(--accent);
  background: var(--accent-a10);
}

.pcard__fam-row--family {
  border-left-color: var(--purple);
  background: color-mix(in srgb, var(--purple) 8%, var(--surface-overlay));
}

.pcard__fam-ava {
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: 6px;
  overflow: hidden;
  display: grid;
  place-items: center;
  font-size: 13px;
  font-weight: 700;
  color: var(--ink-on-fill);
}

.pcard__fam-ava :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.pcard__fam-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pcard__fam-name-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.pcard__fam-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.pcard__fam-role {
  flex: none;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.3;
}

.pcard__fam-role--primary {
  color: var(--accent);
  background: var(--accent-a15);
}

.pcard__fam-role--family {
  color: var(--purple);
  background: color-mix(in srgb, var(--purple) 20%, transparent);
}

.pcard__fam-sub-line {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
}

.pcard__fam-side {
  flex: none;
  display: flex;
  align-items: center;
}

.pcard__fam-region {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 7px;
  border-radius: 6px;
  background: var(--surface-inset);
  border: 1px solid var(--border-soft);
  font-size: 11px;
  color: var(--text-secondary);
}

.pcard__fam-region.is-unset {
  color: var(--text-secondary);
  opacity: 0.7;
}

.pcard__fam-wallet {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

/* 奖杯卡：KPI + 进度 + 白金封面条 */
.pcard__ach-kpi {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.pcard__ach-plat {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.pcard__ach-rate {
  font-size: 12px;
  color: var(--text-secondary);
}

.pcard__ach-track {
  height: 6px;
  border-radius: 999px;
  background: var(--surface-inset);
  overflow: hidden;
}

.pcard__ach-track i {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--accent);
}

.pcard__ach-line {
  font-size: 12px;
  color: var(--text-secondary);
}

.pcard__ach-plats {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  padding-bottom: 2px;
}

.pcard__ach-plat-game {
  flex: none;
  width: 86px;
  aspect-ratio: 460 / 215;
  border-radius: 5px;
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
  font-size: 11px;
  color: var(--text-secondary);
}

.pcard__ach-plat-game :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.pcard__row-rating {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--success);
}

.pcard__row-rating.medium {
  color: var(--warning);
}

.pcard__row-rating.low {
  color: var(--text-secondary);
}

.pcard__row-meta {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-secondary);
}

.pcard__done {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-primary);
}

.pcard__done-dot {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--success);
  box-shadow: 0 0 0 3px var(--accent-a15);
}

.pcard__dellist {
  display: grid;
  gap: 4px;
}

.pcard__delrow {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 12px;
  line-height: 1.5;
}

/* 封面：不在目录的游戏也能凭 appid 拼 Steam 头图——删除确认里的「人脸」 */
.pcard__delcover {
  flex: none;
  width: 56px;
  aspect-ratio: 460 / 215;
  border-radius: 4px;
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
  font-size: 10px;
  color: var(--text-secondary);
}

.pcard__delcover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.pcard__delmain {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.pcard__delname {
  flex: none;
  max-width: 55%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: var(--text-primary);
}

.pcard__delreason {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--warning);
}

.pcard__link {
  margin-left: auto;
  font-size: 12px;
  color: var(--accent);
}

.pcard__records {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.pcard__record {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--surface-inset);
  min-width: 0;
}

.pcard__record-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.pcard__record-k {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.pcard__record-at {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-secondary);
  opacity: 0.8;
}

.pcard__record-body {
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
}

.pcard__badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  line-height: 1.4;
  word-break: break-word;
  max-width: 100%;
  background: var(--bg-card);
  color: var(--text-primary);
  border: 1px solid var(--border-soft);
}

.pcard__badge-dot {
  flex-shrink: 0;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.pcard__badge.is-ok {
  background: var(--success-a15);
  color: var(--success);
  border-color: transparent;
}

.pcard__badge.is-warn {
  background: var(--warning-a15);
  color: var(--warning);
  border-color: transparent;
}

.pcard__badge.is-bad {
  background: var(--danger-a15);
  color: var(--danger);
  border-color: transparent;
}

.pcard__hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
}

.pcard__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.pcard__chip {
  padding: 2px 8px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: var(--surface-inset);
  font-size: 12px;
  color: var(--text-primary);
}

.pcard__chip--more {
  color: var(--text-secondary);
}

.pcard__focusable {
  display: grid;
  gap: 8px;
  border-radius: 8px;
}

.pcard__focusable:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 4px;
}

.pcard__acts {
  display: flex;
  gap: 8px;
}

.pcard__acts :deep(.hl-btn) {
  padding: 5px 14px;
  font-size: 12px;
}
</style>
