<script setup lang="ts">
/**
 * Steam 活动日历页。
 *
 * 数据链：steam_events 域每日同步官方文档页落库 → GET /steam-events 只读
 * 展示（store 与仪表盘倒计时卡共享）。页面分四段：下一个活动倒计时（秒级
 * 跳动）、进行中、即将开始、最近已结束（截尾）。活动名按界面语言取
 * nameZh / nameEn，日期为 PT 活动日口径（YYYY-MM-DD）。
 */
import { computed } from 'vue'

import type { SteamEventItem } from '@/api/client'
import { HlButton, HlEmpty, HlSkeleton } from '@/components/ui'
import { useNow } from '@/composables/useNow'
import { displayRangeIso, eventStartTs } from '@/lib/steamEvents'
import { useI18n } from '@/locales'
import { useLocaleStore } from '@/stores/locale'
import { useSteamEventsStore } from '@/stores/steamEvents'

const { t } = useI18n()
const localeStore = useLocaleStore()
const store = useSteamEventsStore()
store.start()

const now = useNow(1000)

/* 分类章词条 key 静态映射：模板动态拼接 key 会绕开 MessageKey 类型校验，
   check-i18n 也无法核对失效引用 */
const CATEGORY_KEYS = {
  seasonal_sale: 'steamEvents.category.seasonal_sale',
  next_fest: 'steamEvents.category.next_fest',
  themed_fest: 'steamEvents.category.themed_fest',
} as const

function categoryLabel(e: SteamEventItem): string {
  return t(CATEGORY_KEYS[e.category])
}

/** 下一个活动倒计时的分量（秒级跳动） */
const countdown = computed(() => {
  const next = store.payload?.next
  if (!next) return null
  const diff = Math.max(0, eventStartTs(next) - now.value)
  const days = Math.floor(diff / 86_400_000)
  const hours = Math.floor((diff % 86_400_000) / 3_600_000)
  const minutes = Math.floor((diff % 3_600_000) / 60_000)
  const seconds = Math.floor((diff % 60_000) / 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return { days, hours: pad(hours), minutes: pad(minutes), seconds: pad(seconds) }
})

function displayName(e: SteamEventItem): string {
  return localeStore.locale === 'zh-CN' ? e.nameZh || e.nameEn : e.nameEn
}

function rangeText(e: SteamEventItem): string {
  const r = displayRangeIso(e, localeStore.locale)
  return t('steamEvents.dateRange', { start: r.start, end: r.end })
}

function daysUntil(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number)
  // 「今天」用用户本地日历日：展示日期（中文=北京窗口 / 英文=PT 日）与
  // 用户对「还有几天」的直觉都以本地日历为参照
  const nowDate = new Date()
  const today = Date.UTC(nowDate.getFullYear(), nowDate.getMonth(), nowDate.getDate())
  return Math.round((Date.UTC(y, (m || 1) - 1, d || 1) - today) / 86_400_000)
}

/** 状态短语：未来 = N 天后开始 / 进行中 = 剩 N 天（整句词条，中英语序各自成句） */
function statusText(e: SteamEventItem): string {
  const todayIso = new Date().toISOString().slice(0, 10)
  const disp = displayRangeIso(e, localeStore.locale)
  if (disp.start > todayIso) {
    const n = daysUntil(disp.start)
    if (n <= 0) return t('steamEvents.startsToday')
    if (n === 1) return t('steamEvents.startsTomorrow')
    return t('steamEvents.startsIn', { n })
  }
  const n = daysUntil(disp.end)
  if (n <= 0) return t('steamEvents.endsToday')
  return t('steamEvents.endsIn', { n })
}

const live = computed(() => store.payload?.live ?? [])
const upcoming = computed(() => {
  const todayIso = new Date().toISOString().slice(0, 10)
  return (store.payload?.events ?? [])
    .filter((e) => e.start > todayIso && e.key !== store.payload?.next?.key)
    .sort((a, b) => a.start.localeCompare(b.start))
})
/** 最近已结束：截尾展示（完整历史后续有需要再做筛选视图） */
const recentEnded = computed(() => {
  const todayIso = new Date().toISOString().slice(0, 10)
  const horizon = new Date(Date.now() - 90 * 86_400_000).toISOString().slice(0, 10)
  return (store.payload?.events ?? [])
    .filter((e) => e.end < todayIso && e.end >= horizon)
    .sort((a, b) => b.end.localeCompare(a.end))
    .slice(0, 6)
})
</script>

<template>
  <div class="events-page">
    <HlSkeleton v-if="store.state === 'loading'" variant="card" :count="4" class="events-skeleton" />
    <div v-else-if="store.state === 'failed'" class="events-failed">
      <HlEmpty size="md" :text="t('steamEvents.loadFailed')" />
      <HlButton variant="text" @click="store.load()">{{ t('steamEvents.retry') }}</HlButton>
    </div>
    <template v-else>
      <!-- 下一个活动：秒级动态倒计时 -->
      <div v-if="store.payload?.next && countdown" class="card events-hero" data-section="steamEvents.section.hero">
        <div class="events-hero__meta">
          <span class="events-chip events-chip--season">{{ categoryLabel(store.payload.next) }}</span>
          <span class="events-hero__range">{{
            rangeText(store.payload.next)
          }}{{ t('steamEvents.tzNote') }}</span>
        </div>
        <div class="events-hero__name">{{ displayName(store.payload.next) }}</div>
        <div class="events-hero__label">
          {{ t('steamEvents.countdown.label', { name: displayName(store.payload.next) }) }}
        </div>
        <div class="events-countdown" role="timer">
          <span class="events-countdown__block">
            <span class="events-countdown__num">{{ countdown.days }}</span>
            <span class="events-countdown__unit">{{ t('steamEvents.countdown.days') }}</span>
          </span>
          <span class="events-countdown__block">
            <span class="events-countdown__num">{{ countdown.hours }}</span>
            <span class="events-countdown__unit">{{ t('steamEvents.countdown.hours') }}</span>
          </span>
          <span class="events-countdown__block">
            <span class="events-countdown__num">{{ countdown.minutes }}</span>
            <span class="events-countdown__unit">{{ t('steamEvents.countdown.minutes') }}</span>
          </span>
          <span class="events-countdown__block">
            <span class="events-countdown__num">{{ countdown.seconds }}</span>
            <span class="events-countdown__unit">{{ t('steamEvents.countdown.seconds') }}</span>
          </span>
        </div>
      </div>
      <HlEmpty
        v-else
        size="md"
        :text="t('steamEvents.noNext')"
      />

      <!-- 进行中 -->
      <section v-if="live.length > 0" class="events-section">
        <h2 class="events-section__title">{{ t('steamEvents.section.live') }}</h2>
        <div
          v-for="e in live"
          :key="e.key"
          class="card events-row events-row--live"
        >
          <span class="events-row__tag"><span class="events-chip events-chip--season">{{ categoryLabel(e) }}</span></span>
          <span class="events-row__name">{{ displayName(e) }}</span>
          <span class="events-row__dates">{{ rangeText(e) }}</span>
          <span class="events-row__status events-row__status--live">{{ statusText(e) }}</span>
        </div>
      </section>

      <!-- 即将开始 -->
      <section v-if="upcoming.length > 0" class="events-section">
        <h2 class="events-section__title">{{ t('steamEvents.section.upcoming') }}</h2>
        <div v-for="e in upcoming" :key="e.key" class="card events-row">
          <span class="events-row__tag"><span class="events-chip">{{ categoryLabel(e) }}</span></span>
          <span class="events-row__name">{{ displayName(e) }}</span>
          <span class="events-row__dates">{{ rangeText(e) }}</span>
          <span class="events-row__status">{{ statusText(e) }}</span>
        </div>
      </section>

      <!-- 最近已结束（截尾） -->
      <section v-if="recentEnded.length > 0" class="events-section">
        <h2 class="events-section__title events-section__title--muted">{{
          t('steamEvents.section.ended')
        }}</h2>
        <div v-for="e in recentEnded" :key="e.key" class="card events-row events-row--ended">
          <span class="events-row__tag"><span class="events-chip events-chip--muted">{{ categoryLabel(e) }}</span></span>
          <span class="events-row__name">{{ displayName(e) }}</span>
          <span class="events-row__dates">{{ rangeText(e) }}</span>
        </div>
      </section>

      <!-- 同步状态：数据较旧明示（源不可达时旧数据照常展示，不静默） -->
      <p v-if="store.payload?.stale" class="events-stale">
        {{ t('steamEvents.refreshing') }}
        <span v-if="store.payload?.fetchedAt">{{
          t('steamEvents.updatedAt', { time: store.payload.fetchedAt.slice(0, 16).replace('T', ' ') })
        }}</span>
      </p>
    </template>
  </div>
</template>

<style scoped>
.events-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 860px;
  margin: 0 auto;
}

.events-skeleton {
  display: block;
}

/* ─── 倒计时 hero ─── */
.events-hero {
  padding: 22px 24px;
  text-align: center;
}

.events-hero__meta {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  margin-bottom: 10px;
}

.events-hero__range {
  font-size: 13px;
  color: var(--text-muted);
}

.events-hero__name {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
}

.events-hero__label {
  margin-top: 6px;
  font-size: 13px;
  color: var(--text-muted);
}

.events-countdown {
  display: flex;
  align-items: baseline;
  justify-content: space-evenly;
  width: 100%;
  margin-top: 16px;
  font-variant-numeric: tabular-nums;
}

.events-countdown__block {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
}

.events-countdown__num {
  font-size: 52px;
  font-weight: 700;
  line-height: 1.1;
  color: var(--accent);
  min-width: 2ch;
  text-align: center;
}

.events-countdown__unit {
  font-size: 14px;
  color: var(--text-muted);
}

/* ─── 分区与列表行 ─── */
.events-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.events-section__title {
  margin: 0 0 2px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.events-section__title--muted {
  color: var(--text-muted);
}

.events-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
}

.events-row--live {
  border-color: var(--accent);
}

.events-row--ended {
  opacity: 0.62;
}

.events-row__name {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.events-row__dates {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.events-row__status {
  flex-shrink: 0;
  min-width: 86px;
  text-align: right;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.events-row__status--live {
  color: var(--accent);
}

/* ─── 分类章 ─── */
/* 固定宽槽位:章宽随标签长短不一会让名称列起点参差,槽宽取各语言最宽
   标签(EN「Seasonal Sale」),章在槽内居中,名称列从同一起点开始 */
.events-row__tag {
  flex-shrink: 0;
  width: 96px;
  display: inline-flex;
  justify-content: center;
}

.events-chip {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--accent-a15);
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
}

.events-chip--season {
  background: var(--success-a15);
  color: var(--success-on-dark);
}

.events-chip--muted {
  background: var(--bg-soft);
  color: var(--text-muted);
}

.events-failed {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.events-stale {
  margin: 0;
  text-align: center;
  font-size: 12px;
  color: var(--text-muted);
}
</style>
