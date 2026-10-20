<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { crawlApi } from '@/api/client'
import { usePriceStatus } from '@/composables/usePriceStatus'
import { useI18n } from '@/locales'
import { useCrawlStatusStore } from '@/stores/crawlStatus'

import { activityManager } from './activity/activityManager'
import { IDLE_EXPANDED_ID, islandController } from './activity/islandController'
import type { IslandLayoutAction } from './activity/islandController'
import ActivityPanel from './activity/ActivityPanel.vue'
import PrimaryIsland from './activity/PrimaryIsland.vue'
import SecondaryIsland from './activity/SecondaryIsland.vue'
import { IslandLayoutMotion } from './activity/islandLayoutMotion'
import type { Activity } from './activity/types'
import { armTimer, dropNotice, endTask, island, setHovered } from './island'

const router = useRouter()
const crawl = useCrawlStatusStore()
const { t } = useI18n()
const { kind: priceKind, label: priceLabel, color: priceColor, tip: priceTip, detail: priceDetail, stale: priceStale } = usePriceStatus()

const TONE_ICON: Record<string, string> = {
  success: 'check-circle',
  error: 'x-circle',
  warning: 'warning',
  info: 'info',
  progress: 'refresh',
}

const rootEl = ref<HTMLElement | null>(null)
const idleStatusDismissed = ref(false)
const primaryAct = computed(() => islandController.primaryActivity)
const secondaryAct = computed(() => islandController.secondaryActivity)
const expandedAct = computed(() => islandController.expandedActivity)
const legacyNotice = computed(() => island.notices.find((notice) => !activityManager.get('notice:' + notice.id)) ?? null)
const legacyTask = computed(() => island.tasks.find((task) => !activityManager.get('task:' + task.key)) ?? null)
const crawlRunning = computed(() => crawl.running && crawl.total > 0)
const progDone = computed(() => crawl.roundProgress?.done ?? crawl.done)
const progTotal = computed(() => crawl.roundProgress?.total ?? crawl.total)

const mode = computed<'idle' | 'notice' | 'task'>(() => {
  const act = primaryAct.value
  if (act) return act.kind === 'task' || act.status === 'running' ? 'task' : 'notice'
  if (legacyNotice.value) return legacyNotice.value.sticky ? 'task' : 'notice'
  if (legacyTask.value || crawlRunning.value) return 'task'
  return 'idle'
})

const showingPrice = computed(() => !primaryAct.value && !legacyNotice.value && !crawlRunning.value && !legacyTask.value)
const visible = computed(() => !idleStatusDismissed.value && (mode.value !== 'idle' || priceStale.value || islandController.isExpanded.value))
const showCompactMain = computed(() => !(showingPrice.value && islandController.isIdleExpanded))
const waiting = computed(() => islandController.waitingCount)
const isDevExpanded = computed(() => expandedAct.value?.id.startsWith('dev:') ?? false)

const taskPercent = computed<number | null>(() => {
  const act = primaryAct.value
  if (act) return act.progress ?? null
  if (legacyNotice.value?.sticky) return null
  if (legacyTask.value) return legacyTask.value.percent
  if (!progTotal.value) return null
  return Math.min(100, Math.round((progDone.value / progTotal.value) * 100))
})

const crawlOnlyTask = computed(() => {
  const act = primaryAct.value
  if (act) return act.source === 'crawler' && act.status === 'running' && !act.id.startsWith('dev:')
  return !legacyNotice.value && !legacyTask.value && crawlRunning.value
})
const canRetry = computed(() => Boolean(primaryAct.value?.retry || legacyNotice.value?.retry))
const busy = computed(() => {
  const act = primaryAct.value
  if (act) return act.status === 'running' && (act.progress !== undefined || act.kind === 'task')
  return Boolean(legacyNotice.value?.sticky || crawlRunning.value || legacyTask.value)
})
const taskLines = computed(() => island.tasks.slice(1, 4))
const taskGoto = computed(() => legacyTask.value?.to ?? '')

const goto = computed(() => {
  const act = primaryAct.value
  if (act) return act.to || (act.status === 'failed' || act.tone === 'error' ? '/logs' : '')
  const notice = legacyNotice.value
  return notice ? notice.to || (notice.tone === 'error' ? '/logs' : '') : ''
})
const gotoLabel = computed(() => {
  const act = primaryAct.value
  if (act?.toLabel) return act.toLabel
  const notice = legacyNotice.value
  if (notice?.toLabel) return notice.toLabel
  return goto.value === '/logs' ? t('island.viewLogs') : t('island.view')
})

const headText = computed(() => {
  if (showingPrice.value) return priceLabel.value
  if (primaryAct.value) return primaryAct.value.title
  if (legacyNotice.value) return legacyNotice.value.text
  if (legacyTask.value) return legacyTask.value.text
  return t('island.task.running', { done: progDone.value, total: progTotal.value })
})
const headTextStyle = computed(() => showingPrice.value ? { color: priceColor.value } : {})
const headTruncated = computed(() => headText.value.length > 32)
const trailing = computed(() => '')

function iconFor(activity: Activity | null, price: boolean, tone?: string): string {
  if (price) return 'refresh'
  if (activity?.icon) return activity.icon
  if (activity?.tone && TONE_ICON[activity.tone]) return TONE_ICON[activity.tone]
  if (activity?.status === 'failed') return 'x-circle'
  if (activity?.status === 'success') return 'check-circle'
  if (tone && TONE_ICON[tone]) return TONE_ICON[tone]
  return 'refresh'
}

const leadIcon = computed(() => iconFor(primaryAct.value, showingPrice.value, legacyNotice.value?.tone))
const leadSpin = computed(() => {
  if (showingPrice.value) return priceKind.value === 'running'
  if (primaryAct.value) return primaryAct.value.status === 'running'
  if (mode.value === 'task') return true
  return Boolean(legacyNotice.value?.sticky)
})
const leadToneClass = computed(() => {
  if (showingPrice.value) return ''
  const act = primaryAct.value
  if (act?.status === 'failed') return 'is-error'
  if (act?.tone) return 'is-' + act.tone
  if (act) return 'is-progress'
  return legacyNotice.value ? 'is-' + legacyNotice.value.tone : 'is-progress'
})
const showBar = computed(() => {
  if (primaryAct.value) {
    return primaryAct.value.status === 'running' && (primaryAct.value.progress !== undefined || primaryAct.value.kind === 'task')
  }
  return busy.value
})
const detailText = computed(() => primaryAct.value?.detail ?? legacyNotice.value?.detail ?? (showingPrice.value ? priceDetail.value : ''))

const expandedShowingPrice = computed(() => islandController.isIdleExpanded)
const expandedShowsTasks = computed(() => !expandedAct.value && !legacyNotice.value && Boolean(legacyTask.value))
const expandedTaskLines = computed(() => island.tasks.slice(1, 4))
const expandedTaskGoto = computed(() => legacyTask.value?.to ?? '')
const expandedText = computed(() => {
  if (expandedAct.value) return expandedAct.value.title
  if (legacyNotice.value) return legacyNotice.value.text
  if (legacyTask.value) return legacyTask.value.text
  return priceLabel.value
})
const expandedProgress = computed<number | null>(() => {
  if (expandedAct.value) return expandedAct.value.progress ?? null
  if (legacyNotice.value?.sticky) return null
  if (legacyTask.value) return legacyTask.value.percent
  if (crawlRunning.value && progTotal.value) return Math.min(100, Math.round((progDone.value / progTotal.value) * 100))
  return null
})
const expandedIcon = computed(() => iconFor(expandedAct.value, expandedShowingPrice.value, legacyNotice.value?.tone))
const expandedSpin = computed(() => {
  if (expandedShowingPrice.value) return priceKind.value === 'running'
  if (expandedAct.value) return expandedAct.value.status === 'running'
  return Boolean(legacyNotice.value?.sticky || legacyTask.value || crawlRunning.value)
})
const expandedToneClass = computed(() => {
  if (expandedShowingPrice.value) return ''
  if (expandedAct.value?.status === 'failed') return 'is-error'
  if (expandedAct.value?.tone) return 'is-' + expandedAct.value.tone
  if (expandedAct.value) return 'is-progress'
  return legacyNotice.value ? 'is-' + legacyNotice.value.tone : 'is-progress'
})
const expandedIsTask = computed(() => {
  if (expandedAct.value) return expandedAct.value.kind === 'task' && expandedAct.value.source === 'crawler' && !expandedAct.value.id.startsWith('dev:')
  return !legacyNotice.value && crawlRunning.value
})
const expandedCanRetry = computed(() => Boolean(expandedAct.value?.retry || legacyNotice.value?.retry))
const expandedGoto = computed(() => {
  if (expandedAct.value) return expandedAct.value.to || (expandedAct.value.status === 'failed' || expandedAct.value.tone === 'error' ? '/logs' : '')
  if (legacyNotice.value) return legacyNotice.value.to || (legacyNotice.value.tone === 'error' ? '/logs' : '')
  return ''
})
const expandedGotoLabel = computed(() => {
  if (expandedAct.value?.toLabel) return expandedAct.value.toLabel
  if (legacyNotice.value?.toLabel) return legacyNotice.value.toLabel
  return expandedGoto.value === '/logs' ? t('island.viewLogs') : t('island.view')
})
const expandedDetail = computed(() => expandedAct.value?.detail ?? legacyNotice.value?.detail ?? (expandedShowingPrice.value ? priceDetail.value : ''))
const expandedShowBar = computed(() => {
  if (expandedAct.value) return expandedAct.value.status === 'running' && (expandedAct.value.progress !== undefined || expandedAct.value.kind === 'task')
  return Boolean(legacyTask.value || crawlRunning.value)
})
const expandedTrailing = computed(() => {
  if (expandedProgress.value !== null) return String(expandedProgress.value) + '%'
  return waiting.value ? t('island.more', { n: waiting.value }) : ''
})
const expandedHeadLines = computed(() => Math.min(4, headLineCount(expandedText.value, 350)))
const expandedHeadStyle = computed(() => {
  const style: Record<string, string> = { webkitLineClamp: '4' }
  if (expandedShowingPrice.value) style.color = priceColor.value
  return style
})
const expandedHeight = computed(() => {
  const row = Math.max(26, expandedHeadLines.value * 17)
  return 63 + row + (expandedDetail.value ? 39 : 0) + (expandedShowBar.value ? 12 : 0)
})
const expandedStyle = computed(() => ({
  width: 'min(420px, calc(100vw - 24px))',
  height: String(expandedHeight.value) + 'px',
  borderRadius: '24px',
}))

const idleWidth = computed(() => Math.min(240, Math.max(146, 82 + priceLabel.value.length * 13)))
const noticeWidth = computed(() => {
  const title = primaryAct.value?.title ?? legacyNotice.value?.text ?? ''
  return Math.min(320, Math.max(160, 76 + title.length * 13))
})
const size = computed(() => {
  if (islandController.hasSecondary) return { w: 240, h: 36, r: 18 }
  if (mode.value === 'idle') return { w: idleWidth.value, h: 36, r: 18 }
  return { w: noticeWidth.value, h: 36, r: 18 }
})
const islandStyle = computed(() => ({
  width: String(size.value.w) + 'px',
  height: String(size.value.h) + 'px',
  borderRadius: String(size.value.r) + 'px',
}))
const mainDataId = computed(() => primaryAct.value?.id ?? (showingPrice.value ? IDLE_EXPANDED_ID : undefined))

function headLineCount(text: string, wrapWidth: number): number {
  const wideGlyph = /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7FF\uF900-\uFAFA\uFE30-\uFE6F\uFF00-\uFFEF]/
  let width = 0
  for (const character of text) width += wideGlyph.test(character) ? 13 : 10
  return Math.max(1, Math.ceil(width / wrapWidth))
}

let activeMotion: IslandLayoutMotion | null = null
let applyingControllerMutation = false

function syncExpandedBridge() {
  island.expanded = islandController.isExpanded.value
  armTimer()
}

async function transitionLayout(change: () => void) {
  activeMotion?.finish()
  activeMotion = null
  const motion = rootEl.value ? new IslandLayoutMotion(rootEl.value) : null
  activeMotion = motion
  applyingControllerMutation = true
  try {
    change()
  } finally {
    applyingControllerMutation = false
  }
  syncExpandedBridge()
  await nextTick()
  if (activeMotion !== motion) return
  await motion?.play()
  if (activeMotion === motion) activeMotion = null
}

function onIslandClick() {
  void transitionLayout(() => islandController.openMain())
}

function onSecondaryAction() {
  if (!islandController.hasSecondary) return
  void transitionLayout(() => islandController.openSecondary())
}

function returnToPreviousLayout() {
  void transitionLayout(() => islandController.returnPreviousLayout())
}

function collapseExpanded() {
  void transitionLayout(() => islandController.collapseExpanded())
}

function dismiss() {
  const act = expandedAct.value ?? primaryAct.value
  if (act) {
    if (act.id.startsWith('notice:')) dropNotice(Number(act.id.slice('notice:'.length)))
    else if (act.id.startsWith('task:')) endTask(act.id.slice('task:'.length))
    else activityManager.remove(act.id)
    return
  }
  if (legacyNotice.value) {
    dropNotice(legacyNotice.value.id)
    return
  }
  if (showingPrice.value) {
    idleStatusDismissed.value = true
    void transitionLayout(() => islandController.collapseExpanded())
  }
}

function retry() {
  const act = expandedAct.value ?? primaryAct.value
  islandController.collapseExpanded()
  syncExpandedBridge()
  if (act) {
    if (act.id.startsWith('dev:')) {
      activityManager.update(act.id, { status: 'running', tone: 'info', progress: 0 })
      return
    }
    const fn = act.retry ?? null
    activityManager.remove(act.id)
    fn?.()
    return
  }
  const fn = legacyNotice.value?.retry ?? null
  if (legacyNotice.value) dropNotice(legacyNotice.value.id)
  fn?.()
}

function goDetail() {
  const target = expandedGoto.value || goto.value
  if (!target) return
  const isDev = (expandedAct.value ?? primaryAct.value)?.id.startsWith('dev:') ?? false
  islandController.collapseExpanded()
  syncExpandedBridge()
  if (isDev) return
  void router.push(target)
}

function goTask() {
  const target = expandedTaskGoto.value
  if (!target) return
  islandController.collapseExpanded()
  syncExpandedBridge()
  if (isDevExpanded.value) return
  void router.push(target)
}

async function cancelTask() {
  const act = expandedAct.value ?? primaryAct.value
  islandController.collapseExpanded()
  syncExpandedBridge()
  if (act?.id.startsWith('dev:')) {
    activityManager.fail(act.id, { detail: 'Cancelled in test' })
    return
  }
  const id = crawl.activeJobId
  if (id === null) return
  try {
    await crawlApi.stop(id)
  } catch {
    /* 忽略取消请求失败 */
  }
}

function openTask() {
  const act = expandedAct.value ?? primaryAct.value
  islandController.collapseExpanded()
  syncExpandedBridge()
  if (act?.id.startsWith('dev:')) return
  void router.push(act?.to || '/crawl')
}

function openOverview() {
  void transitionLayout(() => islandController.setOverview(true))
}

function onDocDown(event: MouseEvent) {
  if (!islandController.isExpanded.value && !islandController.isOverviewOpen.value) return
  if (rootEl.value?.contains(event.target as Node)) return
  void transitionLayout(() => {
    islandController.collapseExpanded()
    islandController.setOverview(false)
  })
}

watch(islandController.swapRequest, () => onSecondaryAction())
watch(islandController.layoutRequest, () => {
  const action = islandController.takeLayoutAction()
  if (!action) return
  const actions: Record<IslandLayoutAction, () => void> = {
    main: () => islandController.openMain(),
    secondary: () => islandController.openSecondary(),
    collapse: () => islandController.collapseExpanded(),
    return: () => islandController.returnPreviousLayout(),
  }
  void transitionLayout(actions[action])
})
watch(islandController.isExpanded, (expanded) => {
  if (island.expanded === expanded) return
  island.expanded = expanded
  armTimer()
}, { immediate: true })
watch(
  () => [primaryAct.value?.id, legacyNotice.value?.id, legacyTask.value?.key, priceKind.value, priceLabel.value, priceStale.value],
  () => { idleStatusDismissed.value = false },
)
watch(
  () => JSON.stringify(islandController.layout),
  () => {
    if (applyingControllerMutation) return
    activeMotion?.finish()
    activeMotion = null
  },
  { flush: 'sync' },
)

function onSelectActivity(id: string) {
  if (!activityManager.get(id)) return
  void transitionLayout(() => {
    islandController.setOverview(false)
    if (id !== islandController.primaryId) islandController.selectPrimary(id)
  })
}

function onPinActivity(id: string) {
  void transitionLayout(() => islandController.togglePin(id))
}

function onPanelAction(activity: Activity) {
  void transitionLayout(() => islandController.setOverview(false))
  if (activity.to && !activity.id.startsWith('dev:')) void router.push(activity.to)
}

onMounted(() => document.addEventListener('mousedown', onDocDown))
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocDown)
  activeMotion?.finish()
  activeMotion = null
})
</script>

<template>
  <Transition name="island-retract">
    <div
      v-if="visible"
      ref="rootEl"
      class="hl-island-host"
    >
      <!-- 主岛容器 -->
      <div
        v-if="showCompactMain"
        class="hl-island"
        :class="['hl-island--' + mode]"
        :style="islandStyle"
        :data-activity-id="mainDataId"
        data-island-slot="main"
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
          :is-pinned="islandController.pinnedId === primaryAct?.id"
          :lead-spin="leadSpin"
          :lead-icon="leadIcon"
          :lead-tone-class="leadToneClass"
          :showing-price="showingPrice"
          :can-dismiss-status="!islandController.isExpanded.value"
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
          @go-detail="goDetail"
          @go-task="goTask"
          @open-task="openTask"
        />
      </div>

      <!-- 副岛（多活动时浮现，点击交换，支持悬停预览与总览触发） -->
      <Transition name="secondary-island-pop">
        <SecondaryIsland
          v-if="islandController.hasSecondary"
          :activity="secondaryAct"
          :extra-count="islandController.waitingCount"
          @swap="onSecondaryAction"
          @open-overview="openOverview"
        />
      </Transition>

      <div
        v-if="islandController.isExpanded.value"
        class="hl-island hl-expanded-island hl-island--expanded"
        :class="{ 'is-stacked': expandedHeadLines > 1, 'is-idle-expanded': expandedShowingPrice }"
        :style="expandedStyle"
        :data-activity-id="islandController.expandedId"
        data-island-slot="expanded"
        role="group"
        :aria-label="expandedText"
        @click.stop
      >
        <PrimaryIsland
          :activity="expandedAct"
          mode="expanded"
          :is-pinned="islandController.pinnedId === expandedAct?.id"
          :lead-spin="expandedSpin"
          :lead-icon="expandedIcon"
          :lead-tone-class="expandedToneClass"
          :showing-price="expandedShowingPrice"
          :can-dismiss-status="true"
          :price-color="priceColor"
          :head-text="expandedText"
          :head-text-style="expandedHeadStyle"
          :head-truncated="expandedHeadLines >= 4"
          :trailing="expandedTrailing"
          :crawl-only-task="expandedIsTask"
          :show-bar="expandedShowBar"
          :task-percent="expandedProgress"
          :expanded-shows-tasks="expandedShowsTasks"
          :task-lines="expandedTaskLines"
          :task-goto="expandedTaskGoto"
          :detail-text="expandedDetail"
          :expanded-is-task="expandedIsTask"
          :can-retry="expandedCanRetry"
          :goto="expandedGoto"
          :goto-label="expandedGotoLabel"
          @cancel="cancelTask"
          @retry="retry"
          @dismiss="dismiss"
          @go-detail="goDetail"
          @go-task="goTask"
          @open-task="openTask"
          @return="returnToPreviousLayout"
          @collapse="collapseExpanded"
        />
      </div>

      <!-- 活动总览面板 -->
      <ActivityPanel
        :activities="islandController.sortedActivities"
        :primary-id="islandController.primaryId"
        :pinned-id="islandController.pinnedId"
        :open="islandController.isOverviewOpen.value"
        @select="onSelectActivity"
        @pin="onPinActivity"
        @action="onPanelAction"
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
  z-index: 9999;
  width: 0;
  height: 36px;
  user-select: none;
  -webkit-user-select: none;
}

.hl-island-host :deep(*) {
  user-select: none;
  -webkit-user-select: none;
}

.hl-island {
  position: absolute;
  top: 0;
  left: 0;
  transform: translateX(-50%);
  --text-primary: var(--island-shell-fg);
  --text-secondary: var(--island-shell-fg);
  --text-muted: var(--island-shell-muted);
  --island-bg: var(--island-shell-bg);
  --island-btn: var(--island-shell-btn);
  --surface-track: var(--island-shell-track);
  color: var(--island-shell-fg);
  font-family: var(--font-sans);
  background: var(--island-shell-bg);
  cursor: pointer;
  box-shadow: var(--shadow-island);
  border: none;
  outline: none;
  transition:
    width calc(320ms * var(--motion-scale)) cubic-bezier(0.22, 1, 0.36, 1),
    height calc(320ms * var(--motion-scale)) cubic-bezier(0.22, 1, 0.36, 1),
    border-radius calc(320ms * var(--motion-scale)) cubic-bezier(0.22, 1, 0.36, 1);
}

:deep(.hl-secondary-island) {
  position: absolute;
  top: 0;
  left: 128px;
  --text-primary: var(--island-shell-fg);
  --text-secondary: var(--island-shell-fg);
  --text-muted: var(--island-shell-muted);
  --island-btn: var(--island-shell-btn);
  background: var(--island-shell-bg);
  color: var(--island-shell-fg);
}

.hl-expanded-island {
  position: fixed;
  top: calc(var(--island-top) + 44px);
  left: 50%;
  transform: translateX(-50%);
  z-index: 10000;
  max-width: calc(100vw - 24px);
  box-sizing: border-box;
  overflow: hidden;
  cursor: default;
}

.hl-expanded-island.is-idle-expanded {
  top: var(--island-top);
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

:deep(.hl-island__pinned) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
  margin-left: 2px;
  flex: 0 0 auto;
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

:deep(.hl-island__slot-control) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex: 0 0 22px;
  padding: 0;
  border: none;
  border-radius: 50%;
  color: var(--text-muted);
  background: var(--island-btn);
  cursor: pointer;
}

:deep(.hl-island__slot-control:hover) {
  color: var(--text-primary);
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

.island-retract-enter-active,
.island-retract-leave-active {
  transition:
    opacity calc(var(--duration-4) * var(--motion-scale)) var(--ease-out),
    transform calc(var(--duration-4) * var(--motion-scale)) var(--ease-island);
}

.island-retract-enter-from,
.island-retract-leave-to {
  opacity: 0;
  transform: translateY(-12px);
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

  .hl-island {
    transition: none;
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

.hl-island--expanded :deep(.hl-island__lead) {
  border-radius: 9px;
}

.hl-island--expanded :deep(.hl-island__text) {
  font-weight: 600;
  letter-spacing: -0.01em;
}

.hl-island--expanded :deep(.hl-island__detail),
.hl-island--expanded :deep(.hl-island__taskpct) {
  color: var(--island-shell-muted);
}
</style>
