<script setup lang="ts">
/**
 * Steam 活动倒计时卡 —— 仪表盘分节卡，与 Epic 喜加一卡片组同形态。
 *
 * 数据链：GET /steam-events（store 与活动日历页共享，挂载即拉 + 每小时
 * 轮询；无数据源可显时整块隐藏，不打扰空库用户）。卡面只承载「下一个
 * 活动是什么、什么时候开始」：名称 + 秒级倒计时 + 日期区间，整卡站内
 * 跳转活动日历页。倒计时时刻：精确时间戳回填前按 Valve 惯例 10:00 PT 换算
 * （夏令时/冬令时分别对北京时间凌晨 1 点 / 2 点，口径见 lib/steamEvents）。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import type { SteamEventItem } from '@/api/client'
import { HlEmpty, HlSkeleton } from '@/components/ui'
import { useNow } from '@/composables/useNow'
import { displayRangeIso, eventStartTs } from '@/lib/steamEvents'
import { useI18n } from '@/locales'
import { useLocaleStore } from '@/stores/locale'
import { useSteamEventsStore } from '@/stores/steamEvents'

const router = useRouter()
const { t } = useI18n()
const localeStore = useLocaleStore()
const store = useSteamEventsStore()
store.start()

const now = useNow(1000)

const CATEGORY_KEYS = {
  seasonal_sale: 'steamEvents.category.seasonal_sale',
  next_fest: 'steamEvents.category.next_fest',
  themed_fest: 'steamEvents.category.themed_fest',
} as const

function rangeText(e: SteamEventItem): string {
  const r = displayRangeIso(e, localeStore.locale)
  return t('steamEvents.dateRange', { start: r.start, end: r.end })
}

function displayName(e: SteamEventItem): string {
  return localeStore.locale === 'zh-CN' ? e.nameZh || e.nameEn : e.nameEn
}

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

const liveNow = computed(() => store.payload?.live?.[0] ?? null)
</script>

<template>
  <div v-if="store.state !== 'failed'" class="card steam-event" data-section="dashboard.section.steamEvent">
    <div class="steam-event__header">
      <div class="steam-event__title">{{ t('nav.events') }}</div>
      <span v-if="store.payload?.stale" class="steam-event__refreshing">{{
        t('steamEvents.refreshing')
      }}</span>
      <router-link class="steam-event__more" to="/events">{{
        t('steamEvents.goCalendar')
      }}</router-link>
    </div>

    <HlSkeleton v-if="store.state === 'loading'" variant="card" :count="2" class="steam-event__skeleton" />
    <HlEmpty
      v-else-if="!store.payload?.next && !liveNow"
      size="sm"
      :text="t('steamEvents.noNext')"
    />
    <template v-else>
      <!-- 进行中的活动优先呈现（当前可参与），否则展示下一个活动倒计时 -->
      <div v-if="liveNow" class="steam-event__body steam-event__body--live">
        <span class="steam-event__tag"><span class="steam-event__chip">{{ t(CATEGORY_KEYS[liveNow.category]) }}</span></span>
        <div class="steam-event__main">
          <span class="steam-event__name">{{ displayName(liveNow) }}</span>
          <span class="steam-event__range">{{ rangeText(liveNow) }}</span>
        </div>
        <span class="steam-event__live">{{ t('steamEvents.section.live') }}</span>
      </div>
      <div v-if="store.payload?.next && countdown" class="steam-event__body">
        <span class="steam-event__tag"><span class="steam-event__chip steam-event__chip--next">{{
          t(CATEGORY_KEYS[store.payload.next.category])
        }}</span></span>
        <div class="steam-event__main">
          <span class="steam-event__name">{{ displayName(store.payload.next) }}</span>
          <span class="steam-event__label">{{
            t('steamEvents.countdown.label', { name: displayName(store.payload.next) })
          }}</span>
        </div>
        <span class="steam-event__timer" role="timer">
          <span class="steam-event__seg">
            <span class="steam-event__num">{{ countdown.days }}</span>
            <span class="steam-event__unit">{{ t('steamEvents.countdown.days') }}</span>
          </span>
          <span class="steam-event__seg">
            <span class="steam-event__num">{{ countdown.hours }}</span>
            <span class="steam-event__unit">{{ t('steamEvents.countdown.hours') }}</span>
          </span>
          <span class="steam-event__seg">
            <span class="steam-event__num">{{ countdown.minutes }}</span>
            <span class="steam-event__unit">{{ t('steamEvents.countdown.minutes') }}</span>
          </span>
          <span class="steam-event__seg">
            <span class="steam-event__num">{{ countdown.seconds }}</span>
            <span class="steam-event__unit">{{ t('steamEvents.countdown.seconds') }}</span>
          </span>
        </span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.steam-event {
  padding: 18px 20px;
}

.steam-event__header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.steam-event__title {
  flex: 1;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.steam-event__refreshing {
  font-size: 12px;
  color: var(--accent);
}

.steam-event__more {
  font-size: 12px;
  color: var(--accent);
  text-decoration: none;
}

.steam-event__more:hover {
  text-decoration: underline;
}

.steam-event__skeleton {
  display: block;
}

.steam-event__body {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
}

.steam-event__body + .steam-event__body {
  border-top: 1px solid var(--border-soft);
}

.steam-event__body--live {
  border-left: 3px solid var(--accent);
  padding-left: 12px;
}

/* 固定宽槽位:两行（进行中/下一个）的分类章对齐，名称列同起点 */
.steam-event__tag {
  flex-shrink: 0;
  width: 96px;
  display: inline-flex;
  justify-content: center;
}

.steam-event__chip {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--success-a15);
  color: var(--success-on-dark);
  font-size: 11px;
  font-weight: 600;
}

.steam-event__chip--next {
  background: var(--accent-a15);
  color: var(--accent);
}

.steam-event__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.steam-event__name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.steam-event__range,
.steam-event__label {
  font-size: 12px;
  color: var(--text-muted);
}

.steam-event__live {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
}

.steam-event__timer {
  flex-shrink: 0;
  display: inline-flex;
  align-items: baseline;
  gap: 10px;
  font-variant-numeric: tabular-nums;
}

/* 天与时分秒同一字号（用户可读性优先），单位缩小缀在各段后 */
.steam-event__seg {
  display: inline-flex;
  align-items: baseline;
  gap: 2px;
}

.steam-event__num {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.1;
  color: var(--accent);
  min-width: 2ch;
  text-align: center;
}

.steam-event__unit {
  font-size: 11px;
  color: var(--text-muted);
}
</style>
