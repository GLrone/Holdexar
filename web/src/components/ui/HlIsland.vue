<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { crawlApi } from '@/api/client'
import { usePriceStatus } from '@/composables/usePriceStatus'
import { useI18n } from '@/locales'
import { useCrawlStatusStore } from '@/stores/crawlStatus'

import { activityManager } from './activity/activityManager'
import { islandController } from './activity/islandController'
import ActivityPanel from './activity/ActivityPanel.vue'
import PrimaryIsland from './activity/PrimaryIsland.vue'
import SecondaryIsland from './activity/SecondaryIsland.vue'
import type { Activity, ActivityTone } from './activity/types'
import { dropNotice, island, muteTone, setExpanded, setHovered } from './island'

/**
 * 灵动岛宿主组件（HlIsland）：
 * 顶部悬浮的活动胶囊宿主，单活动时呈现经典单岛，多活动时接入副岛与总览面板。
 * 业务活动通过 ActivityManager 统一协调，展示状态由 IslandController 解耦驱动。
 */

const router = useRouter()
const crawl = useCrawlStatusStore()
const { t } = useI18n()

/* 空闲态即价格更新状态：结论与展开面正文同一数据源、同一口径 */
const {
  kind: priceKind,
  label: priceLabel,
  color: priceColor,
  tip: priceTip,
  detail: priceDetail,
  stale: priceStale,
} = usePriceStatus()

const TONE_ICON: Record<string, string> = {
  success: 'check-circle',
  error: 'x-circle',
  warning: 'warning',
  info: 'info',
  progress: 'refresh',
}

/* 四态尺寸 */
const SHAPE = {
  idle: { h: 30, r: 15 },
  notice: { h: 34, r: 17 },
  task: { w: 348, h: 58, r: 20 },
  expanded: { w: 420, r: 28 },
} as const

const EXPANDED_TEXT_W = 350
const EXPANDED_MAX_LINES = 4
const EXPANDED_LINE_H = 17
const EXPANDED_LEAD_H = 26
const EXPANDED_CHROME_H = 63
const EXPANDED_DETAIL_H = 39
const EXPANDED_BAR_H = 12
const WIDE_GLYPH = /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7FF\uF900-\uFAFF\uFE30-\uFE6F\uFF00-\uFFEF]/
const EXPANDED_TRAIL_W = 72

function headLineCount(text: string, wrapWidth: number): number {
  let width = 0
  for (const ch of text) width += WIDE_GLYPH.test(ch) ? 13 : 10
  return Math.max(1, Math.ceil(width / wrapWidth))
}

const DECK_MAX = 2
const rootEl = ref<HTMLElement | null>(null)

/** 当前主岛活动 */
const primaryAct = computed(() => islandController.primaryActivity)

/** 当前副岛活动 */
const secondaryAct = computed(() => islandController.secondaryActivity)

/** 兼容旧版 notices / tasks */
const legacyNotice = computed(() => island.notices[0] ?? null)
const legacyTask = computed(() => island.tasks[0] ?? null)
const crawlRunning = computed(() => crawl.running && crawl.total > 0)

const waiting = computed(() => Math.max(0, activityManager.count - 1))

/** 当前展示模式 */
const mode = computed<'idle' | 'notice' | 'task' | 'expanded'>(() => {
  if (islandController.isExpanded.value || island.expanded) return 'expanded'
  const act = primaryAct.value
  if (act) {
    if (act.kind === 'task' || act.status === 'running') return 'task'
    return 'notice'
  }
  if (legacyNotice.value) {
    return legacyNotice.value.sticky ? 'task' : 'notice'
  }
  if (legacyTask.value || crawlRunning.value) return 'task'
  return 'idle'
})

/** 是否处于价格空闲态 */
const showingPrice = computed(() => !primaryAct.value && !legacyNotice.value && !crawlRunning.value && !legacyTask.value)

/** 岛体可见性 */
const visible = computed(() => mode.value !== 'idle' || priceStale.value)

/* 进度口径 */
const progDone = computed(() => crawl.roundProgress?.done ?? crawl.done)
const progTotal = computed(() => crawl.roundProgress?.total ?? crawl.total)

const taskPercent = computed<number | null>(() => {
  const act = primaryAct.value
  if (act && act.progress !== undefined) return act.progress
  const n = legacyNotice.value
  if (n && n.sticky) return null
  if (legacyTask.value) return legacyTask.value.percent
  if (!progTotal.value) return null
  return Math.min(100, Math.round((progDone.value / progTotal.value) * 100))
})

const crawlOnlyTask = computed(() => {
  const act = primaryAct.value
  if (act) return act.source === 'crawler' && act.status === 'running'
  return !legacyNotice.value && !legacyTask.value && crawlRunning.value
})

const expandedIsTask = computed(() => {
  const act = primaryAct.value
  if (act) return act.kind === 'task' && act.source === 'crawler'
  return !legacyNotice.value && crawlRunning.value
})

const expandedShowsTasks = computed(() => {
  if (primaryAct.value) return false
  return !legacyNotice.value && !!legacyTask.value
})

const canRetry = computed(() => Boolean(primaryAct.value?.retry || legacyNotice.value?.retry))

const busy = computed(() => {
  const act = primaryAct.value
  if (act) return act.status === 'running' || act.progress !== undefined
  return Boolean(legacyNotice.value?.sticky || crawlRunning.value || legacyTask.value)
})

const taskLines = computed(() => {
  return island.tasks.slice(1, 4)
})

const taskGoto = computed(() => legacyTask.value?.to ?? '')

const goto = computed(() => {
  const act = primaryAct.value
  if (act) return act.to || (act.status === 'failed' || act.tone === 'error' ? '/logs' : '')
  const n = legacyNotice.value
  if (!n) return ''
  return n.to || (n.tone === 'error' ? '/logs' : '')
})

const gotoLabel = computed(() => {
  const act = primaryAct.value
  if (act) {
    if (act.toLabel) return act.toLabel
    return goto.value === '/logs' ? t('island.viewLogs') : t('island.view')
  }
  const n = legacyNotice.value
  if (!n) return ''
  if (n.toLabel) return n.toLabel
  return goto.value === '/logs' ? t('island.viewLogs') : t('island.view')
})

const headText = computed(() => {
  if (showingPrice.value) return priceLabel.value
  const act = primaryAct.value
  if (act) return act.title
  const n = legacyNotice.value
  if (n) return n.text
  if (legacyTask.value) return legacyTask.value.text
  return t('island.task.running', { done: progDone.value, total: progTotal.value })
})

const leadIcon = computed(() => {
  if (showingPrice.value) return 'refresh'
  const act = primaryAct.value
  if (act) {
    if (act.icon) return act.icon
    if (act.tone && TONE_ICON[act.tone]) return TONE_ICON[act.tone]
    if (act.status === 'failed') return 'x-circle'
    if (act.status === 'success') return 'check-circle'
    return 'refresh'
  }
  const n = legacyNotice.value
  return n ? TONE_ICON[n.tone] || 'refresh' : 'refresh'
})

const leadSpin = computed(() => {
  if (showingPrice.value) return priceKind.value === 'running'
  const act = primaryAct.value
  if (act) return act.status === 'running'
  if (mode.value === 'task') return true
  return Boolean(legacyNotice.value?.sticky)
})

const leadToneClass = computed(() => {
  if (showingPrice.value) return ''
  const act = primaryAct.value
  if (act) {
    if (act.status === 'failed') return 'is-error'
    if (act.tone) return `is-${act.tone}`
    return 'is-progress'
  }
  const n = legacyNotice.value
  if (!n) return 'is-progress'
  return `is-${n.tone}`
})

const showBar = computed(() => busy.value)

const trailing = computed(() => {
  if (mode.value !== 'expanded') return ''
  if (taskPercent.value !== null) return `${taskPercent.value}%`
  if (activityManager.count > 1) return t('island.more', { n: activityManager.count - 1 })
  if (waiting.value) return t('island.more', { n: waiting.value })
  return ''
})

const detailText = computed(() => {
  const act = primaryAct.value
  if (act && act.detail) return act.detail
  if (legacyNotice.value) return legacyNotice.value.detail ?? ''
  return showingPrice.value ? priceDetail.value : ''
})

const deckDepth = computed(() => Math.min(DECK_MAX, waiting.value))

const idleWidth = computed(() => Math.min(240, Math.max(124, 60 + priceLabel.value.length * 13)))

const noticeWidth = computed(() => {
  const act = primaryAct.value
  const len = act ? act.title.length : (legacyNotice.value?.text.length ?? 0)
  return Math.min(400, Math.max(168, 76 + len * 13))
})

const headWrapWidth = computed(() => EXPANDED_TEXT_W - (trailing.value ? EXPANDED_TRAIL_W : 0))
const headRawLines = computed(() => headLineCount(headText.value, headWrapWidth.value))
const headLines = computed(() => Math.min(EXPANDED_MAX_LINES, headRawLines.value))
const headTruncated = computed(() => headRawLines.value > EXPANDED_MAX_LINES)
const headStacked = computed(() => mode.value === 'expanded' && headLines.value > 1)

const headTextStyle = computed(() => {
  const style: Record<string, string> = {}
  if (showingPrice.value) style.color = priceColor.value
  if (mode.value === 'expanded') style.webkitLineClamp = String(EXPANDED_MAX_LINES)
  return style
})

const expandedHeight = computed(() => {
  const row = Math.max(EXPANDED_LEAD_H, headLines.value * EXPANDED_LINE_H)
  const detail = detailText.value ? EXPANDED_DETAIL_H : 0
  const bar = showBar.value ? EXPANDED_BAR_H : 0
  return EXPANDED_CHROME_H + row + detail + bar
})

const size = computed(() => {
  const m = mode.value
  if (m === 'idle') return { w: idleWidth.value, h: SHAPE.idle.h, r: SHAPE.idle.r }
  if (m === 'notice') return { w: noticeWidth.value, h: SHAPE.notice.h, r: SHAPE.notice.r }
  if (m === 'expanded') return { w: SHAPE.expanded.w, h: expandedHeight.value, r: SHAPE.expanded.r }
  return { w: SHAPE.task.w, h: SHAPE.task.h, r: SHAPE.task.r }
})

const islandStyle = computed(() => ({
  width: `${size.value.w}px`,
  height: `${size.value.h}px`,
  borderRadius: `${size.value.r}px`,
}))

function onIslandClick() {
  const next = !islandController.isExpanded.value
  islandController.setExpanded(next)
  setExpanded(next)
}

function dismiss() {
  islandController.setExpanded(false)
  setExpanded(false)
  const act = primaryAct.value
  if (act) {
    activityManager.remove(act.id)
  }
  const n = legacyNotice.value
  if (n) dropNotice(n.id)
}

function mute() {
  islandController.setExpanded(false)
  setExpanded(false)
  const act = primaryAct.value
  if (act && act.tone) {
    muteTone(act.tone as ActivityTone)
  } else if (legacyNotice.value) {
    muteTone(legacyNotice.value.tone)
  }
}

function retry() {
  const act = primaryAct.value
  const fn = act?.retry ?? legacyNotice.value?.retry ?? null
  islandController.setExpanded(false)
  setExpanded(false)
  if (act) activityManager.remove(act.id)
  if (legacyNotice.value) dropNotice(legacyNotice.value.id)
  fn?.()
}

function goDetail() {
  const target = goto.value
  if (!target) return
  islandController.setExpanded(false)
  setExpanded(false)
  void router.push(target)
}

function goTask() {
  const target = taskGoto.value
  if (!target) return
  islandController.setExpanded(false)
  setExpanded(false)
  void router.push(target)
}

async function cancelTask() {
  const id = crawl.activeJobId
  islandController.setExpanded(false)
  setExpanded(false)
  if (id === null) return
  try {
    await crawlApi.stop(id)
  } catch {
    /* 忽略取消请求失败 */
  }
}

function openTask() {
  islandController.setExpanded(false)
  setExpanded(false)
  void router.push('/crawl')
}

function onDocDown(e: MouseEvent) {
  if (!islandController.isExpanded.value && !island.expanded && !islandController.isOverviewOpen.value) return
  if (rootEl.value?.contains(e.target as Node)) return
  islandController.setExpanded(false)
  islandController.setOverview(false)
  setExpanded(false)
}

function onSecondarySwap() {
  islandController.swap()
}

function onSelectActivity(id: string) {
  islandController.selectPrimary(id)
  islandController.setOverview(false)
}

onMounted(() => document.addEventListener('mousedown', onDocDown))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocDown))
</script>

<template>
  <Transition name="island-retract">
    <div v-if="visible" ref="rootEl" class="hl-island-host">
      <!-- 主岛容器 -->
      <div
        class="hl-island"
        :class="[`hl-island--${mode}`, { 'is-stacked': headStacked }]"
        :style="islandStyle"
        role="status"
        aria-live="polite"
        :aria-label="t('island.label')"
        :title="mode === 'idle' ? priceTip || undefined : undefined"
        @mouseenter="setHovered(true)"
        @mouseleave="setHovered(false)"
        @click="onIslandClick"
      >
        <PrimaryIsland
          :activity="primaryAct"
          :mode="mode"
          :lead-spin="leadSpin"
          :lead-icon="leadIcon"
          :lead-tone-class="leadToneClass"
          :showing-price="showingPrice"
          :price-color="priceColor"
          :head-text="headText"
          :head-text-style="headTextStyle"
          :head-truncated="headTruncated"
          :trailing="trailing"
          :crawl-only-task="crawlOnlyTask"
          :show-bar="showBar"
          :task-percent="taskPercent"
          :expanded-shows-tasks="expandedShowsTasks"
          :task-lines="taskLines"
          :task-goto="taskGoto"
          :detail-text="detailText"
          :expanded-is-task="expandedIsTask"
          :can-retry="canRetry"
          :goto="goto"
          :goto-label="gotoLabel"
          @cancel="cancelTask"
          @retry="retry"
          @dismiss="dismiss"
          @mute="mute"
          @go-detail="goDetail"
          @go-task="goTask"
          @open-task="openTask"
        />

        <!-- 排队堆叠 -->
        <Transition name="island-deck">
          <span v-if="deckDepth" class="hl-island__deck" aria-hidden="true">
            <i v-for="n in deckDepth" :key="n" />
          </span>
        </Transition>
      </div>

      <!-- 副岛脚手架（多活动时浮现，点击交换） -->
      <Transition name="secondary-island-pop">
        <SecondaryIsland
          v-if="islandController.hasSecondary"
          :activity="secondaryAct"
          :extra-count="islandController.waitingCount"
          @swap="onSecondarySwap"
        />
      </Transition>

      <!-- 活动总览面板脚手架 -->
      <ActivityPanel
        :activities="islandController.sortedActivities"
        :primary-id="islandController.primaryId"
        :pinned-id="islandController.pinnedId"
        :open="islandController.isOverviewOpen.value"
        @select="onSelectActivity"
        @close="islandController.setOverview(false)"
      />
    </div>
  </Transition>
</template>

<style scoped>
.hl-island-host {
  position: fixed;
  top: var(--island-top);
  left: 50%;
  transform: translateX(-50%);
  z-index: 9999;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.hl-island {
  position: relative;
  color: var(--text-primary);
  font-family: var(--font-sans);
  background: var(--island-bg);
  cursor: pointer;
  box-shadow: var(--shadow-island);
  transition:
    width calc(var(--duration-5) * var(--motion-scale)) var(--ease-island),
    height calc(var(--duration-5) * var(--motion-scale)) var(--ease-island),
    border-radius calc(var(--duration-5) * var(--motion-scale)) var(--ease-island);
}

:deep(.hl-island__body) {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 0 16px;
  box-sizing: border-box;
}

.hl-island--task :deep(.hl-island__body) {
  gap: 9px;
}

.hl-island--expanded :deep(.hl-island__body) {
  justify-content: flex-start;
  padding: 14px 18px 13px;
}

:deep(.hl-island__row) {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-width: 0;
  flex: 0 0 auto;
}

.hl-island--idle :deep(.hl-island__row) {
  justify-content: center;
}

:deep(.hl-island__lead) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  transition:
    width calc(var(--duration-4) * var(--motion-scale)) var(--ease-island),
    height calc(var(--duration-4) * var(--motion-scale)) var(--ease-island),
    background-color calc(var(--duration-4) * var(--motion-scale)) var(--ease-out);
}

.hl-island--expanded :deep(.hl-island__lead) {
  width: 26px;
  height: 26px;
  border-radius: 8px;
  background: var(--island-btn);
}

:deep(.hl-island__icon) {
  color: var(--text-muted);
  flex: 0 0 auto;
}

:deep(.hl-island__icon.is-success) {
  color: var(--success);
}

:deep(.hl-island__icon.is-error) {
  color: var(--danger);
}

:deep(.hl-island__icon.is-warning) {
  color: var(--warning);
}

:deep(.hl-island__icon.is-info) {
  color: var(--info);
}

:deep(.hl-island__icon.is-progress) {
  color: var(--accent);
}

:deep(.hl-island__spin) {
  width: 12px;
  height: 12px;
  flex: 0 0 auto;
}

:deep(.hl-island__text) {
  font-size: 12px;
  line-height: 1.3;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1 1 auto;
  min-width: 0;
  transition:
    font-size calc(var(--duration-4) * var(--motion-scale)) var(--ease-out),
    color calc(var(--duration-3) * var(--motion-scale)) var(--ease-out);
}

:deep(.hl-island__text.is-status) {
  flex: 0 1 auto;
  font-weight: 500;
}

.hl-island--expanded :deep(.hl-island__text) {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  font-size: 13px;
  font-weight: 500;
  white-space: normal;
  overflow-wrap: anywhere;
  text-overflow: clip;
}

.hl-island--expanded.is-stacked :deep(.hl-island__row) {
  align-items: flex-start;
}

:deep(.hl-island__trail) {
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted);
  flex: 0 0 auto;
  animation: hlIslandDrop 280ms var(--ease-island) 60ms both;
}

:deep(.hl-island__fold) {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 8px;
}

:deep(.hl-island__spacer) {
  flex: 1 1 auto;
}

:deep(.hl-island__detail) {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  animation: hlIslandDrop 320ms var(--ease-island) 40ms both;
}

:deep(.hl-island__tasks) {
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 7px;
}

:deep(.hl-island__taskline) {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  animation: hlIslandDrop 300ms var(--ease-island) both;
}

:deep(.hl-island__tasktext) {
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1 1 auto;
  min-width: 0;
}

:deep(.hl-island__taskpct) {
  font-size: 11px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted);
  flex: 0 0 auto;
}

:deep(.hl-island__bar) {
  position: relative;
  display: block;
  height: 3px;
  border-radius: 2px;
  overflow: hidden;
  background: var(--surface-track);
  flex: 0 0 auto;
}

:deep(.hl-island__bar > i) {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 40%;
  border-radius: 2px;
  background: var(--accent);
}

:deep(.hl-island__bar.is-slide > i) {
  animation: hlIslandBar 1.4s var(--ease-inout) infinite;
}

@keyframes hlIslandBar {
  from {
    transform: translateX(-100%);
  }
  to {
    transform: translateX(350%);
  }
}

:deep(.hl-island__actions) {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}

:deep(.hl-island__actions > *) {
  animation: hlIslandDrop 300ms var(--ease-island) both;
}

:deep(.hl-island__actions > *:nth-child(1)) {
  animation-delay: 100ms;
}

:deep(.hl-island__actions > *:nth-child(2)) {
  animation-delay: 140ms;
}

:deep(.hl-island__actions > *:nth-child(3)) {
  animation-delay: 180ms;
}

:deep(.hl-island__actions > *:nth-child(4)) {
  animation-delay: 220ms;
}

@keyframes hlIslandDrop {
  from {
    opacity: 0;
    transform: translateY(-6px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

:deep(.hl-island__btn) {
  border: none;
  border-radius: 8px;
  padding: 5px 12px;
  font-size: 11.5px;
  font-family: inherit;
  color: var(--text-primary);
  background: var(--island-btn);
  cursor: pointer;
  transition: opacity var(--transition);
}

:deep(.hl-island__btn.is-primary) {
  background: var(--accent-fill);
  color: var(--on-accent-fill);
}

:deep(.hl-island__btn.is-tiny) {
  padding: 3px 9px;
  font-size: 11px;
  flex: 0 0 auto;
}

:deep(.hl-island__btn:hover) {
  opacity: 0.82;
}

:deep(.hl-island__link) {
  border: none;
  background: transparent;
  padding: 4px 2px;
  margin-left: auto;
  font-size: 11.5px;
  font-family: inherit;
  color: var(--text-muted);
  cursor: pointer;
}

:deep(.hl-island__link:hover) {
  color: var(--text-primary);
}

.hl-island__deck {
  position: absolute;
  left: 0;
  right: 0;
  top: 100%;
  margin-top: -5px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  pointer-events: none;
}

.hl-island__deck > i {
  display: block;
  height: 8px;
  border-radius: 4px;
  background: var(--island-bg);
  box-shadow: var(--shadow-island);
}

.hl-island__deck > i:nth-child(1) {
  width: 86%;
}

.hl-island__deck > i:nth-child(2) {
  width: 72%;
  opacity: 0.72;
}

.island-deck-enter-active,
.island-deck-leave-active {
  transition: opacity calc(var(--duration-3) * var(--motion-scale)) var(--ease-out);
}

.island-deck-enter-from,
.island-deck-leave-to {
  opacity: 0;
}

.island-retract-enter-active,
.island-retract-leave-active {
  transition:
    opacity calc(var(--duration-4) * var(--motion-scale)) var(--ease-out),
    transform calc(var(--duration-4) * var(--motion-scale)) var(--ease-island);
}

.island-retract-enter-from,
.island-retract-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(-12px);
}

.secondary-island-pop-enter-active,
.secondary-island-pop-leave-active {
  transition:
    opacity calc(var(--duration-3) * var(--motion-scale)) var(--ease-out),
    transform calc(var(--duration-4) * var(--motion-scale)) var(--ease-island);
}

.secondary-island-pop-enter-from,
.secondary-island-pop-leave-to {
  opacity: 0;
  transform: scale(0.75) translateX(-8px);
}

@media (prefers-reduced-motion: reduce) {
  .secondary-island-pop-enter-active,
  .secondary-island-pop-leave-active {
    transition: opacity calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
  }

  .secondary-island-pop-enter-from,
  .secondary-island-pop-leave-to {
    transform: none;
  }

  :deep(.hl-island__bar.is-slide > i) {
    animation: none;
  }

  :deep(.hl-island__trail),
  :deep(.hl-island__detail),
  :deep(.hl-island__taskline),
  :deep(.hl-island__actions > *) {
    animation: none;
  }
}
</style>
