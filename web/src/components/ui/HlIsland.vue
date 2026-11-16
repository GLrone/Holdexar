<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { usePriceStatus } from '@/composables/usePriceStatus'
import { useI18n } from '@/locales'

import { activityManager } from './activity/activityManager'
import { IDLE_EXPANDED_ID, islandController } from './activity/islandController'
import type { IslandLayoutAction } from './activity/islandController'
import ActivityPanel from './activity/ActivityPanel.vue'
import PrimaryIsland from './activity/PrimaryIsland.vue'
import SecondaryIsland from './activity/SecondaryIsland.vue'
import { IslandLayoutMotion } from './activity/islandLayoutMotion'
import { resolveActivityCardView } from './activity/presentationResolver'
import type { Activity } from './activity/types'

const router = useRouter()
const { t } = useI18n()
const { kind: priceKind, label: priceLabel, color: priceColor, tip: priceTip, detail: priceDetail, stale: priceStale } = usePriceStatus()

const rootEl = ref<HTMLElement | null>(null)
const idleStatusDismissed = ref(false)
/** 空闲价格状态单次占岛时长：过场状态不强留用户视线 */
const PRICE_STATUS_TTL_MS = 30_000
let priceHideTimer: ReturnType<typeof setTimeout> | null = null
const primaryAct = computed(() => islandController.primaryActivity)
const secondaryAct = computed(() => islandController.secondaryActivity)
const expandedAct = computed(() => islandController.expandedActivity)
const mode = computed<'idle' | 'notice' | 'task'>(() => {
  const act = primaryAct.value
  if (!act) return 'idle'
  return act.kind === 'task' || act.status === 'running' ? 'task' : 'notice'
})
const showingPrice = computed(() => !primaryAct.value)
const visible = computed(() =>
  mode.value !== 'idle'
  || islandController.isExpanded.value
  || (priceStale.value && !idleStatusDismissed.value))
const showCompactMain = computed(() => Boolean(primaryAct.value) || !islandController.isExpanded.value)
const waiting = computed(() => islandController.waitingCount)
const headText = computed(() => primaryAct.value?.title ?? priceLabel.value)
const headTextStyle = computed(() => showingPrice.value ? { color: priceColor.value } : {})
const headTruncated = computed(() => headText.value.length > 32)
const detailText = computed(() => primaryAct.value ? primaryAct.value.detail ?? '' : priceDetail.value)

/** 紧凑胶囊与展开卡动作共用同一套派生 */
const primaryView = computed(() => resolveActivityCardView(primaryAct.value))
const expandedShowingPrice = computed(() => islandController.isIdleExpanded)
const expandedView = computed(() => resolveActivityCardView(expandedAct.value, { showingPrice: expandedShowingPrice.value }))
const expandedText = computed(() => expandedAct.value?.title ?? priceLabel.value)
const expandedDetail = computed(() => expandedAct.value?.detail ?? (expandedShowingPrice.value ? priceDetail.value : ''))

const expandedTrailing = computed(() => {
  if (expandedView.value.percent !== null) return String(expandedView.value.percent) + '%'
  return waiting.value ? t('island.more', { n: waiting.value }) : ''
})
const expandedHeadStyle = computed(() => {
  const style: Record<string, string> = { webkitLineClamp: '4' }
  if (expandedShowingPrice.value) style.color = priceColor.value
  return style
})
const expandedStyle = computed(() => ({
  width: 'min(420px, calc(100vw - 24px))',
  borderRadius: '24px',
}))

const idleWidth = computed(() => Math.min(240, Math.max(146, 82 + priceLabel.value.length * 13)))
const noticeWidth = computed(() => {
  const title = primaryAct.value?.title ?? ''
  return Math.min(320, Math.max(160, 76 + title.length * 13))
})
const size = computed(() => {
  if (islandController.hasSecondary) return { w: 240, h: 36, r: 18 }
  if (mode.value === 'idle') return { w: idleWidth.value, h: 36, r: 18 }
  return { w: noticeWidth.value, h: 36, r: 18 }
})
const islandStyle = computed(() => ({
  width: islandController.hasSecondary ? 'min(240px, calc(100vw - 192px))' : String(size.value.w) + 'px',
  height: String(size.value.h) + 'px',
  borderRadius: String(size.value.r) + 'px',
}))
const mainDataId = computed(() => primaryAct.value?.id ?? (showingPrice.value ? IDLE_EXPANDED_ID : undefined))

let activeMotion: IslandLayoutMotion | null = null
let applyingControllerMutation = false

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
  await nextTick()
  if (activeMotion !== motion) return
  await motion?.play()
  if (activeMotion === motion) activeMotion = null
}

async function onIslandClick() {
  await transitionLayout(() => islandController.openMain())
}

async function onIslandKeydown() {
  await onIslandClick()
  rootEl.value?.querySelector<HTMLElement>('[data-island-slot="expanded"] button')?.focus()
}

function onSecondaryAction() {
  if (!islandController.hasSecondary) return
  void transitionLayout(() => islandController.openSecondary())
}

async function collapseExpanded() {
  const restoreFocus = rootEl.value?.querySelector('[data-island-slot="expanded"]')?.contains(document.activeElement)
  await transitionLayout(() => islandController.collapseExpanded())
  if (restoreFocus) rootEl.value?.querySelector<HTMLElement>('[data-island-slot="main"]')?.focus()
}

function dismiss() {
  const act = expandedAct.value ?? primaryAct.value
  if (act) {
    activityManager.remove(act.id)
    return
  }
  if (showingPrice.value) {
    idleStatusDismissed.value = true
    void transitionLayout(() => islandController.collapseExpanded())
  }
}

function retry() {
  const act = expandedAct.value ?? primaryAct.value
  if (!act) return
  islandController.collapseExpanded()
  if (act.id.startsWith('dev:')) {
    activityManager.update(act.id, { status: 'running', tone: 'info', progress: 0 })
    return
  }
  activityManager.retry(act.id)
}

function goDetail() {
  const act = expandedAct.value ?? primaryAct.value
  if (act?.onOpen) {
    islandController.collapseExpanded()
    act.onOpen()
    return
  }
  const target = expandedView.value.target || primaryView.value.target
  if (!target) return
  const isDev = act?.id.startsWith('dev:') ?? false
  islandController.collapseExpanded()
  if (isDev) return
  void router.push(target)
}

function cancelTask(act: Activity | null) {
  const cancel = resolveActivityCardView(act).cancel
  if (!cancel) return
  void transitionLayout(() => {
    if (act?.id === islandController.expandedId) islandController.collapseExpanded()
    cancel()
  })
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
  }
  void transitionLayout(actions[action])
})
watch(
  () => [priceKind.value, priceLabel.value, priceStale.value],
  () => { idleStatusDismissed.value = false },
)
/* 价格空闲结论只在连续可见 30s 内占岛：到点自动收起，价格状态变化才重新出现 */
watch(
  () => showingPrice.value && priceStale.value
    && !islandController.isExpanded.value && !islandController.isOverviewOpen.value,
  (idleShowing) => {
    if (priceHideTimer) {
      clearTimeout(priceHideTimer)
      priceHideTimer = null
    }
    if (idleShowing) {
      priceHideTimer = setTimeout(() => { idleStatusDismissed.value = true }, PRICE_STATUS_TTL_MS)
    }
  },
  { immediate: true },
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
  if (activity.onOpen) {
    activity.onOpen()
    return
  }
  if (activity.to && !activity.id.startsWith('dev:')) void router.push(activity.to)
}

onMounted(() => document.addEventListener('mousedown', onDocDown))
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocDown)
  if (priceHideTimer) {
    clearTimeout(priceHideTimer)
    priceHideTimer = null
  }
  islandController.setHovered(false)
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
      :class="{ 'has-secondary': islandController.hasSecondary }"
      :style="{ '--island-main-width': islandStyle.width }"
      @keydown.esc.stop="collapseExpanded"
      @mouseenter="islandController.setHovered(true)"
      @mouseleave="islandController.setHovered(false)"
    >
      <!-- 主岛容器 -->
      <div
        v-if="showCompactMain"
        class="hl-island"
        :class="['hl-island--' + mode]"
        :style="islandStyle"
        :data-activity-id="mainDataId"
        data-island-slot="main"
        role="button"
        tabindex="0"
        :aria-expanded="islandController.isExpanded.value"
        aria-live="polite"
        :aria-label="t('island.label')"
        :title="mode === 'idle' ? priceTip || undefined : undefined"
        @click="onIslandClick"
        @keydown.enter.self.prevent="onIslandKeydown"
        @keydown.space.self.prevent="onIslandKeydown"
      >
        <PrimaryIsland
          :activity="primaryAct"
          :mode="mode"
          :is-pinned="islandController.pinnedId === primaryAct?.id"
          :price-running="priceKind === 'running'"
          :showing-price="showingPrice"
          :can-dismiss-status="!islandController.isExpanded.value"
          :price-color="priceColor"
          :head-text="headText"
          :head-text-style="headTextStyle"
          :head-truncated="headTruncated"
          :detail-text="detailText"
          @cancel="cancelTask(primaryAct)"
          @retry="retry"
          @dismiss="dismiss"
          @go-detail="goDetail"
        />
      </div>

      <!-- 副岛（多活动时浮现，点击交换，支持悬停预览与总览触发） -->
      <Transition name="secondary-island-pop">
        <SecondaryIsland
          v-if="islandController.hasSecondary"
          :activity="secondaryAct"
          :extra-count="islandController.waitingCount"
          :preview-enabled="!islandController.isExpanded.value && !islandController.isOverviewOpen.value"
          @swap="onSecondaryAction"
          @open-overview="openOverview"
        />
      </Transition>

      <div
        v-if="islandController.isExpanded.value"
        class="hl-island hl-expanded-island hl-island--expanded"
        :class="{ 'is-solo-expanded': !showCompactMain }"
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
          :price-running="priceKind === 'running'"
          :showing-price="expandedShowingPrice"
          :can-dismiss-status="true"
          :price-color="priceColor"
          :head-text="expandedText"
          :head-text-style="expandedHeadStyle"
          :head-truncated="true"
          :trailing="expandedTrailing"
          :detail-text="expandedDetail"
          @cancel="cancelTask(expandedAct)"
          @retry="retry"
          @dismiss="dismiss"
          @go-detail="goDetail"
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
  left: calc(var(--island-main-width) / 2 + 8px);
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

.hl-island:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

@media (max-width: 430px) {
  .has-secondary :deep(.hl-island__btn.is-tiny) {
    display: none;
  }
}

.hl-expanded-island.is-solo-expanded {
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
  position: relative;
  min-height: 92px;
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
  border-radius: 9px;
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
  font-weight: 600;
  letter-spacing: -0.01em;
  white-space: normal;
  overflow-wrap: anywhere;
  text-overflow: clip;
}

.hl-island--expanded :deep(.hl-island__row) {
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
  flex: 0 0 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
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
  :deep(.hl-island__actions > *) {
    animation: none;
  }
}
</style>
