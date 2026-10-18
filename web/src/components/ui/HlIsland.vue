<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
import {
  createRectKeyframes,
  islandSwapDuration,
  islandSwapEasing,
  type IslandMotionRect,
} from './activity/islandMotionGeometry'
import type { Activity } from './activity/types'
import { dropNotice, island, setExpanded, setHovered } from './island'

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

/* 基础尺寸规格：收起态高度恒定 36px，全圆角 18px */
const SHAPE = {
  compact: { h: 36, r: 18 },
  expanded: { w: 420, r: 24 },
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

const isDevPrimary = computed(() => primaryAct.value?.id.startsWith('dev:') ?? false)

const taskPercent = computed<number | null>(() => {
  const act = primaryAct.value
  if (act) {
    if (act.progress !== undefined) return act.progress
    return null
  }
  const n = legacyNotice.value
  if (n && n.sticky) return null
  if (legacyTask.value) return legacyTask.value.percent
  if (!progTotal.value) return null
  return Math.min(100, Math.round((progDone.value / progTotal.value) * 100))
})

const crawlOnlyTask = computed(() => {
  const act = primaryAct.value
  if (act) return act.source === 'crawler' && act.status === 'running' && !act.id.startsWith('dev:')
  return !legacyNotice.value && !legacyTask.value && crawlRunning.value
})

const expandedIsTask = computed(() => {
  const act = primaryAct.value
  if (act) return act.kind === 'task' && act.source === 'crawler' && !act.id.startsWith('dev:')
  return !legacyNotice.value && crawlRunning.value
})

const expandedShowsTasks = computed(() => {
  if (primaryAct.value) return false
  return !legacyNotice.value && !!legacyTask.value
})

const canRetry = computed(() => Boolean(primaryAct.value?.retry || legacyNotice.value?.retry))

const busy = computed(() => {
  const act = primaryAct.value
  if (act) {
    if (act.status !== 'running') return false
    return act.progress !== undefined || act.kind === 'task'
  }
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

const showBar = computed(() => {
  if (primaryAct.value) {
    return primaryAct.value.status === 'running' && (primaryAct.value.progress !== undefined || primaryAct.value.kind === 'task')
  }
  return busy.value
})

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
  if (m === 'expanded') return { w: SHAPE.expanded.w, h: expandedHeight.value, r: SHAPE.expanded.r }
  // 多活动模式时，主岛推荐固定宽度 240px，高度 36px，圆角 18px
  if (islandController.hasSecondary) {
    return { w: 240, h: SHAPE.compact.h, r: SHAPE.compact.r }
  }
  if (m === 'idle') return { w: idleWidth.value, h: SHAPE.compact.h, r: SHAPE.compact.r }
  return { w: Math.min(320, Math.max(160, noticeWidth.value)), h: SHAPE.compact.h, r: SHAPE.compact.r }
})

const islandStyle = computed(() => ({
  width: `${size.value.w}px`,
  height: `${size.value.h}px`,
  borderRadius: `${size.value.r}px`,
}))

function onIslandClick() {
  if (isSwapBusy.value) return
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

function retry() {
  const act = primaryAct.value
  islandController.setExpanded(false)
  setExpanded(false)
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
  const target = goto.value
  if (!target) return
  islandController.setExpanded(false)
  setExpanded(false)
  if (isDevPrimary.value) return
  void router.push(target)
}

function goTask() {
  const target = taskGoto.value
  if (!target) return
  islandController.setExpanded(false)
  setExpanded(false)
  if (isDevPrimary.value) return
  void router.push(target)
}

async function cancelTask() {
  islandController.setExpanded(false)
  setExpanded(false)
  const act = primaryAct.value
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
  islandController.setExpanded(false)
  setExpanded(false)
  if (isDevPrimary.value) return
  void router.push('/crawl')
}

function onDocDown(e: MouseEvent) {
  if (!islandController.isExpanded.value && !island.expanded && !islandController.isOverviewOpen.value) return
  if (rootEl.value?.contains(e.target as Node)) return
  islandController.setExpanded(false)
  islandController.setOverview(false)
  setExpanded(false)
}

const isSwapBusy = ref(false)
const hideSwapRoots = ref(false)
const pendingSelectionId = ref<string | null>(null)
const swapAnimations: Animation[] = []
const swapClones: HTMLElement[] = []
let swapSequence = 0
let expectedSwapIds: string | null = null
let applyingSwap = false

function motionRect(element: HTMLElement): IslandMotionRect {
  const rect = element.getBoundingClientRect()
  return {
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
    borderRadius: getComputedStyle(element).borderRadius,
  }
}

function createMotionClone(element: HTMLElement): HTMLElement {
  const rect = motionRect(element)
  const clone = element.cloneNode(true) as HTMLElement
  clone.removeAttribute('id')
  clone.removeAttribute('data-v-app')
  clone.setAttribute('aria-hidden', 'true')
  clone.inert = true
  clone.querySelectorAll('[id]').forEach((child) => child.removeAttribute('id'))
  clone.querySelectorAll('.hl-secondary-preview').forEach((preview) => preview.remove())
  Object.assign(clone.style, {
    position: 'fixed',
    left: `${rect.left}px`,
    top: `${rect.top}px`,
    right: 'auto',
    bottom: 'auto',
    width: `${rect.width}px`,
    height: `${rect.height}px`,
    borderRadius: rect.borderRadius,
    margin: '0',
    flex: 'none',
    transform: 'none',
    transition: 'none',
    pointerEvents: 'none',
    zIndex: '10002',
  })
  clone.style.opacity = String(
    Number(getComputedStyle(element).opacity) * Number(getComputedStyle(rootEl.value!).opacity),
  )
  document.body.appendChild(clone)
  swapClones.push(clone)
  return clone
}

function clearSwapVisuals() {
  for (const animation of swapAnimations.splice(0)) animation.cancel()
  for (const clone of swapClones.splice(0)) clone.remove()
  hideSwapRoots.value = false
  rootEl.value?.querySelectorAll<HTMLElement>('.hl-island, .hl-secondary-island').forEach((element) => {
    element.classList.remove('is-swap-hidden')
    element.removeAttribute('aria-hidden')
    element.inert = false
    element.style.removeProperty('opacity')
    element.style.removeProperty('transition')
  })
  isSwapBusy.value = false
  expectedSwapIds = null
}

function cancelSwapForStateChange() {
  if (!isSwapBusy.value) return
  swapSequence += 1
  clearSwapVisuals()
}

function waitForTransition(element: HTMLElement, property: string, fallbackMs: number): Promise<void> {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return Promise.resolve()
  return new Promise((resolve) => {
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      element.removeEventListener('transitionend', onTransitionEnd)
      resolve()
    }
    const onTransitionEnd = (event: TransitionEvent) => {
      if (event.target === element && event.propertyName === property) finish()
    }
    const timer = setTimeout(finish, fallbackMs)
    element.addEventListener('transitionend', onTransitionEnd)
  })
}

async function onSecondarySwap() {
  if (isSwapBusy.value || !islandController.hasSecondary) return
  isSwapBusy.value = true
  const run = ++swapSequence
  const host = rootEl.value
  const main = host?.querySelector<HTMLElement>('.hl-island')
  if (!host || !main) {
    islandController.swap()
    isSwapBusy.value = false
    return
  }

  if (islandController.isExpanded.value || island.expanded) {
    const collapse = waitForTransition(main, 'width', 480)
    islandController.setExpanded(false)
    setExpanded(false)
    await collapse
    if (run !== swapSequence) return
  }

  const oldMain = host.querySelector<HTMLElement>('.hl-island')
  const oldSecondary = host.querySelector<HTMLElement>('.hl-secondary-island')
  if (!oldMain || !oldSecondary) {
    islandController.swap()
    isSwapBusy.value = false
    return
  }

  if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
    islandController.swap()
    isSwapBusy.value = false
    return
  }

  const oldMainId = islandController.primaryId
  const mainClone = createMotionClone(oldMain)
  const secondaryClone = createMotionClone(oldSecondary)
  const mainStart = motionRect(oldMain)
  const secondaryStart = motionRect(oldSecondary)
  hideSwapRoots.value = true
  await nextTick()

  applyingSwap = true
  islandController.swap()
  expectedSwapIds = `${islandController.primaryId ?? ''}:${islandController.secondaryId ?? ''}`
  applyingSwap = false
  await nextTick()

  const newMain = host.querySelector<HTMLElement>('.hl-island')
  const newSecondary = host.querySelector<HTMLElement>('.hl-secondary-island')
  const currentIds = `${islandController.primaryId ?? ''}:${islandController.secondaryId ?? ''}`
  if (!newMain || !newSecondary || currentIds !== expectedSwapIds || run !== swapSequence) {
    clearSwapVisuals()
    return
  }

  const mainTarget = motionRect(newMain)
  const secondaryTarget = motionRect(newSecondary)
  const twoActivities = islandController.count === 2 && oldMainId === islandController.secondaryId
  for (const element of [newMain, newSecondary]) {
    element.style.opacity = '0'
    element.style.transition = 'none'
  }
  hideSwapRoots.value = false
  await nextTick()

  const mainFrames = createRectKeyframes(secondaryStart, mainTarget)
  mainFrames[0].offset = 0
  mainFrames[1].offset = 0.78
  mainFrames.push({ ...mainFrames[1], offset: 1, opacity: 0 })

  const mainMotion = secondaryClone.animate(mainFrames, {
    duration: islandSwapDuration,
    easing: islandSwapEasing,
    fill: 'both',
  })
  swapAnimations.push(mainMotion)

  if (twoActivities) {
    const secondaryFrames = createRectKeyframes(mainStart, secondaryTarget)
    secondaryFrames[0].opacity = 1
    secondaryFrames[1].opacity = 0.2
    const secondaryMotion = mainClone.animate(secondaryFrames, {
      duration: islandSwapDuration,
      easing: islandSwapEasing,
      fill: 'both',
    })
    swapAnimations.push(secondaryMotion)
  } else {
    const outgoingMotion = mainClone.animate(
      [{ opacity: 1 }, { opacity: 0 }],
      { duration: islandSwapDuration, easing: islandSwapEasing, fill: 'both' },
    )
    swapAnimations.push(outgoingMotion)
  }

  for (const element of [newMain, newSecondary]) {
    element.removeAttribute('aria-hidden')
    element.inert = false
    const reveal = element.animate(
      [{ opacity: 0 }, { opacity: 1 }],
      {
        delay: islandSwapDuration * 0.68,
        duration: islandSwapDuration * 0.32,
        easing: 'linear',
        fill: 'both',
      },
    )
    swapAnimations.push(reveal)
  }

  try {
    await Promise.all(swapAnimations.map((animation) => animation.finished.catch(() => undefined)))
  } finally {
    if (run === swapSequence) clearSwapVisuals()
  }
}

watch(islandController.swapRequest, () => void onSecondarySwap())
watch(
  () => `${islandController.primaryId ?? ''}:${islandController.secondaryId ?? ''}`,
  (ids) => {
    if (isSwapBusy.value && !applyingSwap && expectedSwapIds && ids !== expectedSwapIds) {
      cancelSwapForStateChange()
    }
  },
  { flush: 'sync' },
)

function onSelectActivity(id: string) {
  if (!activityManager.get(id)) return
  pendingSelectionId.value = id === islandController.primaryId ? null : id
  islandController.setOverview(false)
}

function onActivityPanelAfterLeave() {
  const id = pendingSelectionId.value
  pendingSelectionId.value = null
  if (id && activityManager.get(id)) islandController.selectPrimary(id)
}

function onPinActivity(id: string) {
  islandController.togglePin(id)
}

function onPanelAction(act: Activity) {
  islandController.setOverview(false)
  if (act.to && !act.id.startsWith('dev:')) {
    void router.push(act.to)
  }
}

onMounted(() => document.addEventListener('mousedown', onDocDown))
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocDown)
  swapSequence += 1
  clearSwapVisuals()
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
        class="hl-island"
        :class="[
          `hl-island--${mode}`,
          {
            'is-stacked': headStacked,
            'is-swap-hidden': hideSwapRoots,
          },
        ]"
        :style="islandStyle"
        :data-activity-id="primaryAct?.id"
        :aria-hidden="hideSwapRoots ? 'true' : undefined"
        :inert="hideSwapRoots"
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
          :is-swap-hidden="hideSwapRoots"
          @swap="onSecondarySwap"
          @open-overview="islandController.setOverview(true)"
        />
      </Transition>

      <!-- 活动总览面板 -->
      <ActivityPanel
        :activities="islandController.sortedActivities"
        :primary-id="islandController.primaryId"
        :pinned-id="islandController.pinnedId"
        :open="islandController.isOverviewOpen.value"
        @select="onSelectActivity"
        @after-close="onActivityPanelAfterLeave"
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
  border: none;
  outline: none;
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

.hl-island.is-swap-hidden {
  visibility: hidden;
  pointer-events: none;
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
