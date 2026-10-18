<script setup lang="ts">
/**
 * IslandPlayground — 灵动岛多活动开发实验室
 * 仅在开发环境中通过 UI 入口拉起，用于直观验证多活动生命周期、优先级排队、
 * 主副岛交互（P2）与展示稳定性。所有模拟注入严格使用 `dev:` 命名空间，
 * 不触碰真实数据库、网络请求或生产状态。
 */
import { computed, onBeforeUnmount, ref } from 'vue'

import HlButton from '@/components/ui/HlButton.vue'
import HlDrawer from '@/components/ui/HlDrawer.vue'
import HlIcon from '@/components/ui/HlIcon.vue'
import { pilotCoverUrl } from '@/lib/pilotView'
import { useI18n } from '@/locales'

import { setExpanded } from '../island'
import { activityManager } from './activityManager'
import { islandController } from './islandController'
import type { Activity, ActivityRegistration } from './types'

const emit = defineEmits<{ (e: 'close'): void }>()
const { t } = useI18n()

const open = ref(true)

function handleClose() {
  open.value = false
  emit('close')
}

/* ── 事件日志 ── */
interface LogItem {
  id: number
  time: string
  text: string
}
const logs = ref<LogItem[]>([])
let logSeq = 0

function addLog(text: string) {
  const d = new Date()
  const time = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
  logs.value.unshift({ id: ++logSeq, time, text })
  if (logs.value.length > 50) {
    logs.value.pop()
  }
}

function clearLog() {
  logs.value = []
}

/* ── 订阅活动变化以记录日志 ── */
const unsubscribe = activityManager.subscribe(() => {
  // 响应式监听
})

/* ── 连续进度定时器 ── */
const continuousTimer = ref<ReturnType<typeof setInterval> | null>(null)
const continuousProgress = ref(0)
const iconCycleTimer = ref<ReturnType<typeof setInterval> | null>(null)

function stopContinuous() {
  if (continuousTimer.value) {
    clearInterval(continuousTimer.value)
    continuousTimer.value = null
  }
}

function stopIconCycle() {
  if (iconCycleTimer.value) {
    clearInterval(iconCycleTimer.value)
    iconCycleTimer.value = null
  }
}

onBeforeUnmount(() => {
  unsubscribe()
  stopContinuous()
  stopIconCycle()
})

/* ── 仅清理 dev: 命名空间的活动 ── */
function clearDevActivities() {
  stopContinuous()
  stopIconCycle()
  const devActs = activityManager.activities.filter((a) => a.id.startsWith('dev:'))
  for (const act of devActs) {
    activityManager.remove(act.id)
  }
  addLog(`Cleared ${devActs.length} dev activities`)
}

/* ── 预设场景注入（全使用 dev: 前缀） ── */
function applyPreset(preset: string) {
  clearDevActivities()

  switch (preset) {
    case 'clear':
      break

    case 'single':
      activityManager.register({
        id: 'dev:crawl',
        source: 'crawler',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockCrawlTitle', { done: 45, total: 100 }),
        detail: t('island.playground.mockCrawlDetail'),
        progress: 45,
        priority: 30,
        tone: 'progress',
      })
      addLog('Injected preset: Single activity (dev:crawl, 45%)')
      break

    case 'dual':
      activityManager.register({
        id: 'dev:crawl',
        source: 'crawler',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockCrawlTitle', { done: 45, total: 100 }),
        detail: t('island.playground.mockCrawlDetail'),
        progress: 45,
        priority: 30,
        tone: 'progress',
      })
      activityManager.register({
        id: 'dev:updater',
        source: 'updater',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockUpdaterTitle'),
        detail: t('island.playground.mockUpdaterDetail', { pct: 80 }),
        progress: 80,
        priority: 35,
        tone: 'accent',
      })
      addLog('Injected preset: Dual activities (dev:crawl + dev:updater)')
      break

    case 'quad':
      activityManager.register({
        id: 'dev:crawl',
        source: 'crawler',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockCrawlTitle', { done: 60, total: 100 }),
        progress: 60,
        priority: 30,
      })
      activityManager.register({
        id: 'dev:updater',
        source: 'updater',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockUpdaterTitle'),
        progress: 75,
        priority: 35,
      })
      activityManager.register({
        id: 'dev:backup',
        source: 'backup',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockBackupTitle'),
        progress: 30,
        priority: 20,
      })
      activityManager.register({
        id: 'dev:notice',
        source: 'notice',
        kind: 'notice',
        status: 'attention',
        title: t('island.playground.mockNoticeTitle'),
        priority: 25,
        tone: 'accent',
      })
      addLog('Injected preset: 4 activities (tests secondary badge +2)')
      break

    case 'ten':
      for (let i = 1; i <= 12; i++) {
        activityManager.register({
          id: `dev:queue-${i}`,
          source: 'batch',
          kind: 'task',
          status: 'running',
          title: t('island.playground.mockBatchTitle', { n: i }),
          progress: Math.floor(Math.random() * 90) + 10,
          priority: 20 + (i % 5),
        })
      }
      addLog('Injected preset: 12 long-queue activities')
      break

    case 'crawl':
      activityManager.register({
        id: 'dev:crawl',
        source: 'crawler',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockCrawlTitle', { done: 60, total: 100 }),
        detail: t('island.playground.mockCrawlDetail'),
        progress: 60,
        priority: 30,
        tone: 'progress',
      })
      addLog('Injected preset: Price Crawl')
      break

    case 'updater':
      activityManager.register({
        id: 'dev:updater',
        source: 'updater',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockUpdaterTitle'),
        detail: t('island.playground.mockUpdaterDetail', { pct: 88 }),
        progress: 88,
        priority: 35,
        tone: 'accent',
      })
      addLog('Injected preset: Updater Download')
      break

    case 'sync':
      activityManager.register({
        id: 'dev:bills-sync',
        source: 'bills',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockSyncTitle'),
        detail: t('island.playground.mockSyncDetail'),
        progress: 64,
        priority: 32,
      })
      addLog('Injected preset: Bills Sync')
      break

    case 'business-mix':
      activityManager.register({
        id: 'dev:mixed-crawl',
        source: 'crawler',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockCrawlTitle', { done: 48, total: 100 }),
        progress: 48,
        priority: 38,
      })
      activityManager.register({
        id: 'dev:mixed-sync',
        source: 'bills',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockSyncTitle'),
        progress: 64,
        priority: 34,
      })
      activityManager.register({
        id: 'dev:mixed-updater',
        source: 'updater',
        kind: 'task',
        status: 'running',
        title: t('island.playground.mockUpdaterTitle'),
        progress: 82,
        priority: 30,
      })
      addLog('Injected preset: Crawl, bills sync, and update')
      break

    case 'updater-available':
      activityManager.register({
        id: 'dev:updater-available',
        source: 'updater',
        kind: 'notice',
        status: 'attention',
        title: t('island.playground.mockUpdateAvailableTitle'),
        metadata: { type: 'available' },
        priority: 42,
      })
      addLog('Injected preset: Update Available')
      break

    case 'updater-ready':
      activityManager.register({
        id: 'dev:updater-ready',
        source: 'updater',
        kind: 'system',
        status: 'attention',
        title: t('island.playground.mockUpdateReadyTitle'),
        metadata: { type: 'ready' },
        priority: 45,
      })
      addLog('Injected preset: Update Ready')
      break

    case 'updater-failed':
      activityManager.register({
        id: 'dev:updater-failed',
        source: 'updater',
        kind: 'task',
        status: 'failed',
        title: t('island.playground.mockUpdateFailedTitle'),
        detail: t('island.playground.mockUpdateFailedDetail'),
        priority: 45,
      })
      addLog('Injected preset: Update Failed')
      break

    case 'progress':
      startContinuousProgress({
        id: 'dev:progress-demo',
        source: 'download',
        initialProgress: 15,
        title: t('island.playground.mockDownloadTitle'),
        priority: 25,
        completionTitle: t('island.playground.mockDownloadDoneTitle'),
        completionDetail: t('island.playground.mockDownloadDoneDetail'),
      })
      addLog('Started dynamic progress preset at 15%')
      break

    case 'alert':
      activityManager.register({
        id: 'dev:proxies-err',
        source: 'proxypool',
        kind: 'system',
        status: 'failed',
        needsAttention: true,
        priority: 99,
        title: t('island.playground.mockProxyAlertTitle'),
        detail: t('island.playground.mockProxyAlertDetail'),
        tone: 'error',
      })
      addLog('Injected preset: Proxy Alert (Top Priority & needsAttention)')
      break

    case 'notice':
      activityManager.register({
        id: 'dev:price-notice',
        source: 'notice',
        kind: 'notice',
        status: 'attention',
        title: t('island.playground.mockNoticeTitle'),
        detail: t('island.playground.mockNoticeDetail'),
        iconImageUrl: pilotCoverUrl(1245620),
        priority: 40,
        tone: 'accent',
      })
      addLog('Injected preset: Historical Price Notice')
      break
  }
}

/* ── 极端情况与边缘测试 ── */
function toggleIconCycle() {
  if (iconCycleTimer.value) {
    stopIconCycle()
    addLog('Stopped icon transition demo')
    return
  }

  clearDevActivities()
  const steps: ActivityRegistration[] = [
    {
      id: 'dev:icon-cycle',
      source: 'crawler',
      kind: 'task',
      status: 'running',
      title: t('island.playground.mockCrawlTitle', { done: 45, total: 100 }),
      detail: t('island.playground.mockCrawlDetail'),
      icon: 'refresh',
      iconImageUrl: '',
      progress: 45,
      priority: 50,
    },
    {
      id: 'dev:icon-cycle',
      source: 'bills',
      kind: 'task',
      status: 'running',
      title: t('island.playground.mockSyncTitle'),
      detail: t('island.playground.mockSyncDetail'),
      icon: 'database',
      iconImageUrl: '',
      progress: 64,
      priority: 50,
    },
    {
      id: 'dev:icon-cycle',
      source: 'updater',
      kind: 'task',
      status: 'running',
      title: t('island.playground.mockUpdaterTitle'),
      detail: t('island.playground.mockUpdaterDetail', { pct: 78 }),
      icon: 'download',
      iconImageUrl: '',
      progress: 78,
      priority: 50,
    },
    {
      id: 'dev:icon-cycle',
      source: 'price-alert',
      kind: 'notice',
      status: 'attention',
      title: t('island.playground.mockNoticeTitle'),
      detail: t('island.playground.mockNoticeDetail'),
      icon: 'bell',
      iconImageUrl: pilotCoverUrl(1245620),
      progress: null,
      priority: 50,
    },
  ]

  let stepIndex = 0
  const applyStep = () => activityManager.register(steps[stepIndex])
  applyStep()
  iconCycleTimer.value = setInterval(() => {
    stepIndex = (stepIndex + 1) % steps.length
    applyStep()
  }, 1400)
  addLog('Started icon transition demo')
}

function startContinuousProgress(options: {
  id: string
  source: string
  initialProgress: number
  title: string
  priority: number
  completionTitle: string
  completionDetail: string
  titleForProgress?: (progress: number) => string
}) {
  continuousProgress.value = options.initialProgress
  activityManager.register({
    id: options.id,
    source: options.source,
    kind: 'task',
    status: 'running',
    title: options.titleForProgress?.(continuousProgress.value) ?? options.title,
    progress: continuousProgress.value,
    priority: options.priority,
  })

  continuousTimer.value = setInterval(() => {
    continuousProgress.value = Math.min(100, continuousProgress.value + 5)
    const progress = continuousProgress.value
    if (progress >= 100) {
      activityManager.complete(options.id, {
        title: options.completionTitle,
        detail: options.completionDetail,
      })
      stopContinuous()
      addLog('Continuous progress completed at 100%')
    } else {
      activityManager.update(options.id, {
        progress,
        ...(options.titleForProgress ? { title: options.titleForProgress(progress) } : {}),
      })
    }
  }, 250)
}

function toggleContinuousProgress() {
  if (continuousTimer.value) {
    stopContinuous()
    addLog('Stopped continuous progress')
    return
  }

  startContinuousProgress({
    id: 'dev:continuous',
    source: 'simulation',
    initialProgress: 0,
    title: t('island.playground.mockContinuousTitle'),
    priority: 50,
    completionTitle: t('island.playground.mockContinuousDone'),
    completionDetail: t('island.playground.mockContinuousDoneDetail'),
    titleForProgress: (progress) => t('island.playground.mockContinuousTitlePct', { pct: progress }),
  })
  addLog('Started continuous progress simulation')
}

function simulateDuplicateUpdate() {
  const existing = activityManager.get('dev:dup-test')
  if (!existing) {
    activityManager.register({
      id: 'dev:dup-test',
      source: 'test',
      kind: 'task',
      status: 'running',
      title: t('island.playground.mockDupTitle'),
      progress: 20,
      priority: 35,
    })
    addLog('Registered dev:dup-test with progress 20%')
  } else {
    const nextProg = Math.min(100, (existing.progress ?? 0) + 20)
    activityManager.register({
      id: 'dev:dup-test',
      source: 'test',
      kind: 'task',
      status: 'running',
      title: t('island.playground.mockDupTitlePct', { pct: nextProg }),
      progress: nextProg,
      priority: 35,
    })
    addLog(`Re-registered dev:dup-test in-place (${nextProg}%), createdAt preserved`)
  }
}

function simulateOutOrderEvent() {
  const id = 'dev:crawl'
  if (!activityManager.get(id)) {
    applyPreset('crawl')
  }
  activityManager.update(id, {
    progress: 25,
    title: t('island.playground.mockOutOrderTitle'),
  })
  addLog('Simulated out-of-order event on dev:crawl (progress set to 25%)')
}

function simulateSuddenEnd() {
  const list = activityManager.activities.filter((a) => a.id.startsWith('dev:'))
  if (list.length === 0) {
    addLog('No dev activities to terminate')
    return
  }
  const target = list[0]
  activityManager.remove(target.id)
  addLog(`Suddenly removed activity: ${target.id}`)
}

function triggerSwap() {
  if (!islandController.hasSecondary) {
    addLog('Add a second activity before testing the island swap')
    return
  }
  islandController.requestSwap()
  addLog('Requested the primary and secondary island transition')
}

function toggleIslandExpanded() {
  const expanded = !islandController.isExpanded.value
  setExpanded(expanded)
  addLog(expanded ? 'Expanded the primary island' : 'Collapsed the primary island')
}

function toggleActivityOverview() {
  const open = !islandController.isOverviewOpen.value
  islandController.setOverview(open)
  addLog(open ? 'Opened the activity overview' : 'Closed the activity overview')
}

/* ── 表格行快捷操作 ── */
function adjustProgress(act: Activity, delta: number) {
  const current = act.progress ?? 0
  const next = Math.max(0, Math.min(100, current + delta))
  activityManager.update(act.id, { progress: next })
  addLog(`Updated ${act.id} progress to ${next}%`)
}

function completeActivity(act: Activity) {
  activityManager.complete(act.id, {
    title: `${act.title}${t('island.playground.mockSuccessSuffix')}`,
  })
  addLog(`Completed activity: ${act.id}`)
}

function failActivity(act: Activity) {
  activityManager.fail(act.id, {
    title: `${act.title}${t('island.playground.mockFailSuffix')}`,
  })
  addLog(`Marked failed: ${act.id}`)
}

function removeActivity(act: Activity) {
  activityManager.remove(act.id)
  addLog(`Removed activity: ${act.id}`)
}

/* 当前活动明细列表 */
const activeList = computed(() => activityManager.activities)

/* ── 阶段真实验收测试 ── */
const p1Verified = ref(false)
const p2Verified = ref(false)
const p3Verified = ref(false)
const isVerifying = ref(false)

async function runLiveAcceptanceCheck() {
  if (isVerifying.value) return
  isVerifying.value = true
  p1Verified.value = false
  p2Verified.value = false
  p3Verified.value = false
  addLog('── Starting Live Acceptance Validation ──')

  try {
    // 1. 清理环境
    clearDevActivities()
    await new Promise((r) => setTimeout(r, 60))

    // 2. 验证 P1: 注册单任务与明确进度
    activityManager.register({
      id: 'dev:test-p1',
      source: 'crawler',
      kind: 'task',
      status: 'running',
      title: 'P1 Live Validation Task',
      progress: 45,
      priority: 30,
    })
    await new Promise((r) => setTimeout(r, 60))
    const p1Item = activityManager.get('dev:test-p1')
    if (!p1Item || p1Item.progress !== 45 || islandController.primaryId !== 'dev:test-p1') {
      throw new Error('P1 task registration or progress assertion failed')
    }

    // 3. 验证 P1: 完成状态清除进度条（消除误显示）
    activityManager.complete('dev:test-p1', { title: 'P1 Completed' })
    await new Promise((r) => setTimeout(r, 60))
    const p1Completed = activityManager.get('dev:test-p1')
    if (p1Completed && p1Completed.progress !== undefined) {
      throw new Error('P1 progress cleanup on complete failed: progress still lingers')
    }
    p1Verified.value = true
    addLog('✓ P1 Activity Model & Progress Clearing: PASSED')

    // 4. 验证 P2: 双任务与副岛建立
    clearDevActivities()
    await new Promise((r) => setTimeout(r, 60))

    activityManager.register({
      id: 'dev:task-a',
      source: 'test',
      kind: 'task',
      status: 'running',
      title: 'Task Alpha',
      priority: 40,
    })
    activityManager.register({
      id: 'dev:task-b',
      source: 'test',
      kind: 'task',
      status: 'running',
      title: 'Task Beta',
      priority: 20,
    })
    await new Promise((r) => setTimeout(r, 60))
    if (!islandController.hasSecondary || islandController.primaryId !== 'dev:task-a' || islandController.secondaryId !== 'dev:task-b') {
      throw new Error('P2 dual task island controller assignment failed')
    }

    // 5. 验证 P2: 主副岛交换
    islandController.swap()
    await new Promise((r) => setTimeout(r, 60))
    if (islandController.primaryId !== 'dev:task-b' || islandController.secondaryId !== 'dev:task-a') {
      throw new Error('P2 island swap failed')
    }

    // 6. 验证 P2: 固定 (Pin) 与抢占 (Attention)
    islandController.pin('dev:task-b')
    await new Promise((r) => setTimeout(r, 60))
    if (islandController.pinnedId !== 'dev:task-b') {
      throw new Error('P2 pin failed')
    }

    // 注入更高优先级但非紧急的任务，固定项仍应保持主岛
    activityManager.register({
      id: 'dev:task-c',
      source: 'test',
      kind: 'task',
      status: 'running',
      title: 'Task Gamma',
      priority: 90,
    })
    await new Promise((r) => setTimeout(r, 60))
    if (islandController.primaryId !== 'dev:task-b') {
      throw new Error('P2 pinned activity was wrongly preempted by non-urgent task')
    }

    // 注入紧急任务，应优先置顶抢占主岛
    activityManager.register({
      id: 'dev:urgent-alert',
      source: 'system',
      kind: 'system',
      status: 'failed',
      needsAttention: true,
      priority: 99,
      title: 'Urgent Alert',
    })
    await new Promise((r) => setTimeout(r, 60))
    if (islandController.primaryId !== 'dev:urgent-alert') {
      throw new Error('P2 urgent needsAttention failed to preempt pinned activity')
    }

    p2Verified.value = true
    addLog('✓ P2 Multi-activity Swap, Pin & Priority Preemption: PASSED')

    // 7. 验证 P3: 活动总览面板状态与直接选定主岛
    islandController.setOverview(true)
    if (!islandController.isOverviewOpen.value) {
      throw new Error('P3 overview open failed')
    }
    islandController.selectPrimary('dev:task-b')
    if (islandController.primaryId !== 'dev:task-b') {
      throw new Error('P3 select primary from overview failed')
    }
    islandController.setOverview(false)
    p3Verified.value = true
    addLog('✓ P3 Overview Hub & Direct Selection: PASSED')

    addLog('All Live Checks Passed')
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    addLog(`✗ Acceptance check failed: ${msg}`)
  } finally {
    islandController.unpin()
    islandController.setOverview(false)
    clearDevActivities()
    isVerifying.value = false
  }
}
</script>

<template>
  <HlDrawer
    v-model="open"
    :modal="false"
    :with-header="false"
    width="580px"
    :top="56"
    class="island-playground-drawer"
  >
    <div class="island-playground">
      <!-- 头部与关闭 -->
      <div class="island-playground__header">
        <div class="island-playground__title-wrap">
          <div class="island-playground__title">
            <HlIcon name="terminal" :size="16" />
            <span>{{ t('island.playground.title') }}</span>
          </div>
          <div class="island-playground__subtitle">
            {{ t('island.playground.subtitle') }}
          </div>
        </div>
        <HlButton variant="text" size="sm" @click="handleClose">
          {{ t('island.playground.close') }}
        </HlButton>
      </div>

      <!-- 预设场景快速注入 -->
      <div class="island-playground__section">
        <div class="island-playground__section-title">
          {{ t('island.playground.presets') }}
        </div>
        <div class="island-playground__btn-grid">
          <HlButton size="sm" @click="applyPreset('crawl')">
            {{ t('island.playground.presetCrawl') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('sync')">
            {{ t('island.playground.presetSync') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('updater')">
            {{ t('island.playground.presetUpdater') }}
          </HlButton>
          <HlButton size="sm" variant="primary" @click="applyPreset('business-mix')">
            {{ t('island.playground.presetBusinessMix') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('updater-available')">
            {{ t('island.playground.presetUpdateAvailable') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('updater-ready')">
            {{ t('island.playground.presetUpdateReady') }}
          </HlButton>
          <HlButton size="sm" variant="warning" @click="applyPreset('updater-failed')">
            {{ t('island.playground.presetUpdateFailed') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('notice')">
            {{ t('island.playground.presetNotice') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('single')">
            {{ t('island.playground.presetSingle') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('dual')">
            {{ t('island.playground.presetDual') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('quad')">
            {{ t('island.playground.presetQuad') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('ten')">
            {{ t('island.playground.presetTen') }}
          </HlButton>
          <HlButton size="sm" @click="applyPreset('progress')">
            {{ t('island.playground.presetProgress') }}
          </HlButton>
          <HlButton size="sm" variant="warning" @click="applyPreset('alert')">
            {{ t('island.playground.presetAlert') }}
          </HlButton>
          <HlButton size="sm" variant="danger" @click="applyPreset('clear')">
            {{ t('island.playground.presetClear') }}
          </HlButton>
        </div>
      </div>

      <!-- 阶段能力状态矩阵 -->
      <div class="island-playground__section">
        <div class="island-playground__section-header">
          <span class="island-playground__section-title">{{ t('island.playground.phaseStatus') }}</span>
          <HlButton
            size="sm"
            :variant="p1Verified && p2Verified ? 'primary' : 'default'"
            :disabled="isVerifying"
            @click="runLiveAcceptanceCheck"
          >
            {{ isVerifying ? t('island.playground.verifying') : t('island.playground.runVerify') }}
          </HlButton>
        </div>
        <div class="island-playground__matrix">
          <div class="matrix-card" :class="p1Verified ? 'is-pass' : 'is-unverified'">
            <span class="matrix-card__name">{{ t('island.playground.p1Status') }}</span>
            <span class="matrix-card__badge" :class="p1Verified ? 'is-pass' : 'is-unverified'">
              {{ p1Verified ? t('island.playground.statusVerified') : t('island.playground.statusUnverified') }}
            </span>
          </div>
          <div class="matrix-card" :class="p2Verified ? 'is-pass' : 'is-unverified'">
            <span class="matrix-card__name">{{ t('island.playground.p2Status') }}</span>
            <span class="matrix-card__badge" :class="p2Verified ? 'is-pass' : 'is-unverified'">
              {{ p2Verified ? t('island.playground.statusVerified') : t('island.playground.statusUnverified') }}
            </span>
          </div>
          <div class="matrix-card" :class="p3Verified ? 'is-pass' : 'is-unverified'">
            <span class="matrix-card__name">{{ t('island.playground.p3Status') }}</span>
            <span class="matrix-card__badge" :class="p3Verified ? 'is-pass' : 'is-unverified'">
              {{ p3Verified ? t('island.playground.statusVerified') : t('island.playground.statusUnverified') }}
            </span>
          </div>
        </div>
      </div>

      <!-- 当前状态监视器 -->
      <div class="island-playground__section">
        <div class="island-playground__section-title">
          {{ t('island.playground.inspector') }}
        </div>
        <div class="island-playground__inspector">
          <div class="inspector-item">
            <span class="inspector-item__label">{{ t('island.playground.primaryLabel') }}</span>
            <span class="inspector-item__val is-highlight">{{ islandController.primaryId ?? t('island.playground.none') }}</span>
          </div>
          <div class="inspector-item">
            <span class="inspector-item__label">{{ t('island.playground.secondaryLabel') }}</span>
            <span class="inspector-item__val">{{ islandController.secondaryId ?? t('island.playground.none') }}</span>
          </div>
          <div class="inspector-item">
            <span class="inspector-item__label">{{ t('island.playground.totalCount') }}</span>
            <span class="inspector-item__val">{{ islandController.count }}</span>
          </div>
          <div class="inspector-item">
            <span class="inspector-item__label">{{ t('island.playground.waitingCount') }}</span>
            <span class="inspector-item__val">{{ islandController.waitingCount }}</span>
          </div>
          <div class="inspector-item">
            <span class="inspector-item__label">{{ t('island.playground.pinnedId') }}</span>
            <span class="inspector-item__val is-muted">{{ islandController.pinnedId ?? t('island.playground.none') }}</span>
          </div>
        </div>
      </div>

      <div class="island-playground__section">
        <div class="island-playground__section-title">
          {{ t('island.playground.visualControls') }}
        </div>
        <div class="island-playground__btn-grid">
          <HlButton size="sm" @click="toggleIslandExpanded">
            {{ islandController.isExpanded.value ? t('island.playground.collapseIsland') : t('island.playground.expandIsland') }}
          </HlButton>
          <HlButton size="sm" @click="toggleActivityOverview">
            {{ islandController.isOverviewOpen.value ? t('island.playground.closeOverview') : t('island.playground.openOverview') }}
          </HlButton>
          <HlButton size="sm" variant="primary" @click="triggerSwap">
            {{ t('island.playground.simSwap') }}
          </HlButton>
        </div>
      </div>

      <!-- 极端状态与操作模拟 -->
      <div class="island-playground__section">
        <div class="island-playground__section-title">
          {{ t('island.playground.manualOps') }}
        </div>
        <div class="island-playground__btn-grid">
          <HlButton
            size="sm"
            :variant="iconCycleTimer ? 'warning' : 'primary'"
            @click="toggleIconCycle"
          >
            {{ iconCycleTimer ? t('island.playground.simIconCycleStop') : t('island.playground.simIconCycle') }}
          </HlButton>
          <HlButton
            size="sm"
            :variant="continuousTimer ? 'warning' : 'default'"
            @click="toggleContinuousProgress"
          >
            {{ continuousTimer ? t('island.playground.simContinuousStop') : t('island.playground.simContinuous') }}
          </HlButton>
          <HlButton size="sm" @click="simulateDuplicateUpdate">
            {{ t('island.playground.simDuplicate') }}
          </HlButton>
          <HlButton size="sm" @click="simulateOutOrderEvent">
            {{ t('island.playground.simOutOrder') }}
          </HlButton>
          <HlButton size="sm" @click="simulateSuddenEnd">
            {{ t('island.playground.simSuddenEnd') }}
          </HlButton>
          <HlButton size="sm" variant="ghost" @click="clearDevActivities">
            {{ t('island.playground.clearDevOnly') }}
          </HlButton>
        </div>
      </div>

      <!-- 活动明细表格 -->
      <div class="island-playground__section">
        <div class="island-playground__section-title">
          {{ t('island.playground.activityList') }} ({{ activeList.length }})
        </div>
        <div v-if="activeList.length === 0" class="island-playground__empty">
          {{ t('island.playground.noActivities') }}
        </div>
        <div v-else class="island-playground__table-wrap">
          <table class="island-playground__table">
            <thead>
              <tr>
                <th>{{ t('island.playground.colId') }}</th>
                <th>{{ t('island.playground.colTitle') }}</th>
                <th>{{ t('island.playground.colStatus') }}</th>
                <th>{{ t('island.playground.colProgress') }}</th>
                <th>{{ t('island.playground.colPriority') }}</th>
                <th>{{ t('island.playground.colActions') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="act in activeList" :key="act.id">
                <td class="col-id">{{ act.id }}</td>
                <td class="col-title" :title="act.title">{{ act.title }}</td>
                <td>
                  <span class="status-pill" :class="`is-${act.status}`">{{ act.status }}</span>
                </td>
                <td>{{ act.progress !== null && act.progress !== undefined ? `${act.progress}%` : '—' }}</td>
                <td>{{ act.priority }}</td>
                <td class="col-actions">
                  <button type="button" class="mini-btn" @click="adjustProgress(act, 10)">
                    {{ t('island.playground.actProgUp') }}
                  </button>
                  <button type="button" class="mini-btn" @click="adjustProgress(act, -10)">
                    {{ t('island.playground.actProgDown') }}
                  </button>
                  <button type="button" class="mini-btn is-success" @click="completeActivity(act)">
                    {{ t('island.playground.actSuccess') }}
                  </button>
                  <button type="button" class="mini-btn is-danger" @click="failActivity(act)">
                    {{ t('island.playground.actFail') }}
                  </button>
                  <button type="button" class="mini-btn is-remove" @click="removeActivity(act)">
                    {{ t('island.playground.actRemove') }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 活动事件日志 -->
      <div class="island-playground__section">
        <div class="island-playground__section-header">
          <span class="island-playground__section-title">{{ t('island.playground.eventLog') }}</span>
          <HlButton size="sm" variant="text" @click="clearLog">
            {{ t('island.playground.clearLog') }}
          </HlButton>
        </div>
        <div class="island-playground__logs">
          <div v-if="logs.length === 0" class="island-playground__empty">
            {{ t('island.playground.logEmpty') }}
          </div>
          <div v-for="log in logs" :key="log.id" class="log-row">
            <span class="log-time">{{ log.time }}</span>
            <span class="log-text">{{ log.text }}</span>
          </div>
        </div>
      </div>
    </div>
  </HlDrawer>
</template>

<style scoped>
.island-playground {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px 20px 32px;
  color: var(--text-primary);
  font-size: 13px;
}

.island-playground__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  border-bottom: 1px solid var(--border-soft);
  padding-bottom: 12px;
}

.island-playground__title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.island-playground__subtitle {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 4px;
}

.island-playground__section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.island-playground__section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.island-playground__section-title {
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--text-muted);
}

.island-playground__matrix {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}

.matrix-card {
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.matrix-card__name {
  font-size: 11px;
  color: var(--text-muted);
}

.matrix-card__badge {
  font-size: 12px;
  font-weight: 600;
}

.matrix-card__badge.is-pass {
  color: var(--color-success);
}

.matrix-card__badge.is-unverified {
  color: var(--text-muted);
}

.matrix-card__badge.is-pending {
  color: var(--text-muted);
}

.island-playground__inspector {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  padding: 10px;
  border-radius: 8px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
}

.inspector-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.inspector-item__label {
  font-size: 11px;
  color: var(--text-muted);
}

.inspector-item__val {
  font-size: 13px;
  font-weight: 600;
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.inspector-item__val.is-highlight {
  color: var(--color-primary);
}

.inspector-item__val.is-muted {
  color: var(--text-muted);
}

.island-playground__btn-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.island-playground__empty {
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
  padding: 16px;
  background: var(--bg-card);
  border-radius: 6px;
  border: 1px dashed var(--border-soft);
}

.island-playground__table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  max-height: 220px;
}

.island-playground__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}

.island-playground__table th,
.island-playground__table td {
  padding: 6px 8px;
  text-align: left;
  border-bottom: 1px solid var(--border-soft);
  white-space: nowrap;
}

.island-playground__table th {
  background: var(--bg-card);
  color: var(--text-muted);
  font-weight: 600;
  position: sticky;
  top: 0;
  z-index: 1;
}

.col-id {
  font-family: var(--font-mono);
  max-width: 90px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.col-title {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.status-pill {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
}

.status-pill.is-running {
  background: var(--bg-card-hover);
  color: var(--color-primary);
}

.status-pill.is-success {
  background: var(--bg-card-hover);
  color: var(--color-success);
}

.status-pill.is-failed {
  background: var(--bg-card-hover);
  color: var(--color-danger);
}

.status-pill.is-attention {
  background: var(--bg-card-hover);
  color: var(--color-warning);
}

.status-pill.is-pending {
  background: var(--bg-card-hover);
  color: var(--text-muted);
}

.col-actions {
  display: flex;
  gap: 4px;
}

.mini-btn {
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
  color: var(--text-primary);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 10px;
  cursor: pointer;
  transition: opacity calc(var(--duration-1) * var(--motion-scale));
}

.mini-btn:hover {
  opacity: 0.8;
}

.mini-btn.is-success {
  color: var(--color-success);
  border-color: var(--color-success);
}

.mini-btn.is-danger {
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.mini-btn.is-remove {
  color: var(--text-muted);
}

.island-playground__logs {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  padding: 8px 10px;
  max-height: 140px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.log-row {
  display: flex;
  gap: 8px;
}

.log-time {
  color: var(--text-muted);
  flex-shrink: 0;
}

.log-text {
  color: var(--text-primary);
  word-break: break-all;
}
</style>
