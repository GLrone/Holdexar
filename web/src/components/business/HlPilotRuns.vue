<script setup lang="ts">
/* 后台任务的只读汇总与停止请求，保留会话列和标签栏两个挂点。 */
import { onBeforeUnmount, ref } from 'vue'

import { agentApi, type AgentRunView } from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import { message } from '@/components/ui'

const props = withDefaults(defineProps<{ variant?: 'side' | 'tab' }>(), { variant: 'tab' })

const { t } = useI18n()
const open = ref(false)
const runs = ref<AgentRunView[]>([])
const busyId = ref('')
const loadFailed = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

const RUN_TERMINAL = new Set(['done', 'failed', 'cancelled', 'budget_exhausted'])

async function loadRuns() {
  try {
    const data = await agentApi.listRuns(undefined, 20)
    runs.value = data.items
    loadFailed.value = false
  } catch {
    loadFailed.value = true
  }
}

function stopPoll() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

/** 面板开合：打开即拉一次并起轮询，关闭即停（不留后台定时器） */
function toggle() {
  open.value = !open.value
  if (open.value) {
    void loadRuns()
    stopPoll()
    timer = setInterval(() => {
      if (open.value) void loadRuns()
    }, 5000)
  } else {
    stopPoll()
  }
}

/** 任务名：job 型按 job id 映射（i18n 词条），无映射回落 id 原文；再退 task / runner */
function runLabel(r: AgentRunView): string {
  const meta = (r.meta ?? {}) as Record<string, unknown>
  const job = typeof meta.job === 'string' ? meta.job : ''
  if (job) {
    const key = `pilot.runs.job.${job}` as MessageKey
    const text = t(key)
    return text === key ? (r.run_id.startsWith('crawl:') ? t('pilot.runs.crawl') : job) : text
  }
  const task = typeof meta.task === 'string' ? meta.task : ''
  if (task) {
    const key = `pilot.runs.job.${task}` as MessageKey
    const text = t(key)
    return text === key ? task : text
  }
  return r.runner
}

/** 状态词条：未登记的机器码原样显示（不猜） */
function runStatusLabel(r: AgentRunView): string {
  if (r.meta?.domainStatus === 'partial') return t('pilot.runs.status.partial')
  if (r.meta?.domainStatus === 'completed' || (r.run_id.startsWith('crawl:') && r.status === 'done')) return t('pilot.runs.status.ended')
  const key = `pilot.runs.status.${r.status}` as MessageKey
  const text = t(key)
  return text === key ? r.status : text
}

/** 耗时 / 在途时长：job 型 create 即 start，以 started_at 优先、回落 created_at */
function runDuration(r: AgentRunView): string {
  const start = r.started_at || r.created_at
  if (!start) return ''
  const begin = Date.parse(start)
  const end = r.finished_at ? Date.parse(r.finished_at) : Date.now()
  if (!Number.isFinite(begin) || !Number.isFinite(end)) return ''
  const sec = Math.max(0, Math.round((end - begin) / 1000))
  const span = sec >= 60 ? `${Math.floor(sec / 60)}m ${sec % 60}s` : `${sec}s`
  return t(r.finished_at ? 'pilot.runs.duration' : 'pilot.runs.runningFor', { n: span })
}

/** 缺少来源的历史任务明确显示未知。 */
function runTriggerLabel(r: AgentRunView): string {
  const meta = (r.meta ?? {}) as Record<string, unknown>
  const entry = typeof meta.entry === 'string' ? meta.entry : r.trigger
  if (entry === 'scheduled') return t('pilot.runs.triggerScheduled')
  if (entry === 'manual') return t('pilot.runs.triggerManual')
  if (entry === 'automatic') return t('pilot.runs.triggerAutomatic')
  return t('pilot.runs.triggerUnknown')
}

function canCancel(r: AgentRunView): boolean {
  return !RUN_TERMINAL.has(r.status) && r.meta?.cancellable === true
}

function runProgress(r: AgentRunView): string {
  const progress = r.meta?.progress as Record<string, unknown> | undefined
  if (!progress) return ''
  const counts = typeof progress.done === 'number' && typeof progress.total === 'number'
    ? `${progress.done}/${progress.total}` : ''
  const failed = typeof progress.failed === 'number'
    ? t('pilot.runs.failedCount', { n: progress.failed }) : ''
  return [counts, failed].filter(Boolean).join(' · ')
}

async function cancelRun(r: AgentRunView) {
  if (busyId.value) return
  busyId.value = r.run_id
  try {
    const result = await agentApi.cancelRun(r.run_id, 'user')
    if (result.accepted) message.success(t('pilot.runs.cancelled'))
    await loadRuns()
  } catch {
    message.error(t('pilot.runs.cancelFailed'))
  } finally {
    busyId.value = ''
  }
}

/** 点面板与触发器之外即收起（面板挂在两个不同挂点，按容器判定而非单 ref） */
function onDocClick(e: MouseEvent) {
  if (!open.value) return
  const el = e.target as Element | null
  if (el?.closest?.('.pilot-runs')) return
  open.value = false
  stopPoll()
}

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  stopPoll()
})

document.addEventListener('click', onDocClick)
</script>

<template>
  <div
    class="pilot-runs"
    :class="`is-${props.variant}`"
  >
    <button
      v-if="props.variant === 'tab'"
      type="button"
      class="pilot-runs__btn pilot-runs__btn--tab"
      :class="{ 'is-open': open }"
      :title="t('pilot.runs.title')"
      @click="toggle"
    >
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M4 7h16M4 12h16M4 17h10" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
      </svg>
    </button>
    <button
      v-else
      type="button"
      class="pilot-runs__btn pilot-runs__btn--side"
      :class="{ 'is-open': open }"
      :title="t('pilot.runs.title')"
      @click="toggle"
    >
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M4 7h16M4 12h16M4 17h10" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
      </svg>
    </button>
    <Transition name="hl-pop">
      <div v-if="open" class="pilot-runs__menu">
        <div class="pilot-runs__head">
          <span class="pilot-runs__title">{{ t('pilot.runs.title') }}</span>
          <button
            type="button"
            class="pilot-runs__refresh"
            :title="t('pilot.runs.refresh')"
            @click="loadRuns"
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </div>
        <div v-if="loadFailed" class="pilot-runs__empty" role="status">{{ t('pilot.runs.loadFailed') }}</div>
        <div v-if="!runs.length && !loadFailed" class="pilot-runs__empty">{{ t('pilot.runs.empty') }}</div>
        <div v-for="r in runs" :key="r.run_id" class="pilot-runs__row">
          <span class="pilot-runs__dot" :class="`is-${r.meta?.domainStatus === 'partial' ? 'partial' : r.status}`" aria-hidden="true"></span>
          <span class="pilot-runs__main">
            <span class="pilot-runs__name">{{ runLabel(r) }}</span>
            <span class="pilot-runs__meta">
              {{ runStatusLabel(r) }} · {{ runDuration(r) }} · {{ runTriggerLabel(r) }}
              <span v-if="runProgress(r)"> · {{ runProgress(r) }}</span>
            </span>
          </span>
          <button
            v-if="canCancel(r)"
            type="button"
            class="pilot-runs__cancel"
            :disabled="busyId === r.run_id"
            :title="busyId === r.run_id ? t('pilot.runs.cancelling') : t('pilot.runs.cancel')"
            @click="cancelRun(r)"
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
            </svg>
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.pilot-runs {
  position: relative;
  flex: none;
}

/* 触发器：两个挂点同语言（方钮 + 虚线边），尺寸按宿主列/标签栏各取一档 */
.pilot-runs__btn {
  display: grid;
  place-items: center;
  padding: 0;
  border: 1px dashed var(--border-soft);
  border-radius: 8px;
  background: none;
  color: var(--text-secondary);
  cursor: pointer;
}

.pilot-runs__btn--tab {
  width: 28px;
  height: 28px;
}

.pilot-runs__btn--side {
  width: 26px;
  height: 26px;
}

.pilot-runs__btn:hover,
.pilot-runs__btn.is-open {
  border-style: solid;
  border-color: var(--accent-a30);
  background: var(--accent-a10);
  color: var(--accent);
}

.pilot-runs__btn svg {
  width: 14px;
  height: 14px;
}

.pilot-runs__menu {
  position: absolute;
  top: calc(100% + 6px);
  z-index: 6;
  width: 300px;
  max-height: 340px;
  overflow-y: auto;
  padding: 4px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
  scrollbar-width: thin;
  scrollbar-color: var(--scroll-thumb) transparent;
}

/* 标签栏在右端 → 面板右对齐；会话列在左端 → 面板左对齐（否则溢出视口） */
.pilot-runs.is-tab .pilot-runs__menu {
  right: 0;
}

.pilot-runs.is-side .pilot-runs__menu {
  left: 0;
}

.pilot-runs__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 6px 6px;
}

.pilot-runs__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.pilot-runs__refresh {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: none;
  color: var(--text-secondary);
  cursor: pointer;
}

.pilot-runs__refresh:hover {
  background: var(--accent-a10);
  color: var(--accent);
}

.pilot-runs__refresh svg {
  width: 13px;
  height: 13px;
}

.pilot-runs__empty {
  padding: 14px 8px;
  font-size: 12px;
  color: var(--text-secondary);
  text-align: center;
}

.pilot-runs__row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 8px;
}

.pilot-runs__row:hover {
  background: var(--accent-a10);
}

/* 状态点：运行/排队 accent（运行中脉动）、完成 success、失败/超预算 danger、取消中性 */
.pilot-runs__dot {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-dim);
}

.pilot-runs__dot.is-running {
  background: var(--accent);
  animation: pilot-runs-pulse 1.2s ease-in-out infinite;
}

.pilot-runs__dot.is-queued {
  background: var(--accent);
  opacity: 0.5;
}

.pilot-runs__dot.is-awaiting_approval,
.pilot-runs__dot.is-partial {
  background: var(--warning);
}

.pilot-runs__dot.is-done {
  background: var(--success);
}

.pilot-runs__dot.is-failed,
.pilot-runs__dot.is-budget_exhausted {
  background: var(--danger);
}

.pilot-runs__dot.is-cancelled {
  background: var(--text-dim);
}

@keyframes pilot-runs-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}

@media (prefers-reduced-motion: reduce) {
  .pilot-runs__dot.is-running {
    animation: none;
  }
}

.pilot-runs__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.pilot-runs__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text-primary);
}

.pilot-runs__meta {
  font-size: 11px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.pilot-runs__cancel {
  flex: none;
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}

.pilot-runs__cancel:hover:not(:disabled) {
  background: var(--accent-a10);
  color: var(--warning);
}

.pilot-runs__cancel:disabled {
  opacity: 0.4;
}

.pilot-runs__cancel svg {
  width: 12px;
  height: 12px;
}
</style>
