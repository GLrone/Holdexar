<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import type { PilotFacts, PilotPhase, PilotStep } from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import HlPilotCards from '@/components/business/HlPilotCards.vue'
import HlPilotThinking from '@/components/business/HlPilotThinking.vue'
import HlSkeleton from '@/components/ui/HlSkeleton.vue'
import { HlIcon } from '@/components/ui'
import { fmtTokens } from '@/lib/pilotView'

/** 流式期随阶段内嵌的过程卡（思考时序原位，done 后由回答层正式版接管） */
interface ChainLiveCard {
  key: number
  phase: number
  card: PilotFacts | null
}

const props = withDefaults(
  defineProps<{
    steps: PilotStep[]
    phases?: PilotPhase[]
    think?: string
    thinkSec?: number | null
    elapsedSec?: number | null
    memory?: { tokens?: number | null; budget?: number | null; archived?: number | null; cached?: boolean } | null
    live?: boolean
    waitingText?: string
    hidePhaseText?: boolean
    failed?: boolean
    liveCards?: ChainLiveCard[]
    busyPid?: string | null
  }>(),
  {
    phases: () => [],
    think: '',
    thinkSec: null,
    elapsedSec: null,
    memory: null,
    live: false,
    waitingText: '',
    hidePhaseText: false,
    failed: false,
    liveCards: () => [],
    busyPid: null,
  },
)

const emit = defineEmits<{
  proposal: [payload: { pid: string; approve: boolean }]
}>()

const { t } = useI18n()

const viewPhases = computed<PilotPhase[]>(() => {
  if (props.phases.length) return props.phases
  if (props.think || props.steps.length || props.live) {
    return [{ step: 0, thinking: props.think, text: '', think_ms: props.thinkSec ? props.thinkSec * 1000 : null, steps: props.steps }]
  }
  return []
})

const activeIndex = computed(() => (props.live && viewPhases.value.length ? viewPhases.value.length - 1 : -1))

/** 本阶段的过程卡：随阶段时序内嵌展示（骨架或实体） */
function phaseCards(pi: number): ChainLiveCard[] {
  return props.liveCards.filter((e) => e.phase === pi)
}

function phaseSec(p: PilotPhase): number | null {
  return p.think_ms ? Math.max(1, Math.round(p.think_ms / 1000)) : null
}

const stepTotal = computed(() => viewPhases.value.reduce((n, p) => n + p.steps.length, 0))

const open = ref(props.live)
const userTouched = ref(false)

function toggle() {
  userTouched.value = true
  open.value = !open.value
}

watch(
  () => props.live,
  (live, was) => {
    if (was && !live && !userTouched.value && !props.failed) open.value = false
  },
)

const liveSec = ref(0)
let startedAt = 0
let ticker: ReturnType<typeof setInterval> | null = null

function stopTicker() {
  if (ticker) {
    clearInterval(ticker)
    ticker = null
  }
}

watch(
  () => props.live,
  (live) => {
    if (live) {
      startedAt = Date.now()
      stopTicker()
      ticker = setInterval(() => {
        liveSec.value = Math.floor((Date.now() - startedAt) / 1000)
      }, 1000)
    } else {
      stopTicker()
    }
  },
  { immediate: true },
)
onBeforeUnmount(stopTicker)

const headLabel = computed(() => {
  if (props.live) {
    return liveSec.value >= 1 ? t('pilot.chain.live', { sec: liveSec.value }) : t('pilot.chain.livePlain')
  }
  if (props.elapsedSec && stepTotal.value) return t('pilot.chain.done', { sec: props.elapsedSec, steps: stepTotal.value })
  if (props.elapsedSec) return t('pilot.chain.doneNoSteps', { sec: props.elapsedSec })
  if (stepTotal.value) return t('pilot.chain.stepsOnly', { steps: stepTotal.value })
  return t('pilot.chain.plain')
})

const memoryText = computed(() => {
  const m = props.memory
  if (!m) return ''
  const parts: string[] = []
  if (m.tokens && m.budget) parts.push(t('pilot.memory.ctx', { ctx: `${fmtTokens(m.tokens)}/${fmtTokens(m.budget)}` }))
  if (m.archived && m.archived > 0) parts.push(t('pilot.memory.archived', { n: m.archived }))
  if (m.cached) parts.push(t('pilot.memory.cachedHit'))
  return parts.join(' · ')
})

/* 词条缺键回落通用词：t 回落链末端返回 key 本身，不把 key 渲染给用户 */
function dictLabel(key: MessageKey): string {
  const text = t(key)
  return text === key ? t('pilot.step.generic') : text
}

function navModuleName(target: string): string {
  return dictLabel(`pilot.nav.${target}` as MessageKey)
}

function plainInline(s: string): string {
  return s.replace(/\*\*([^*]+)\*\*/g, '$1').replace(/==([^=]+)==/g, '$1').replace(/`([^`]+)`/g, '$1')
}

/* 阶段正文展示值：剥标记并去首尾空白——模型常吐纯换行（如 '\n\n'），
   pre-wrap 会把它如实渲染成整块空白，夹在思考行与工具行之间像断层 */
function phasePre(p: PilotPhase): string {
  return plainInline(p.text).trim()
}

function stepText(s: PilotStep): string {
  if (s.status === 'denied') return t('pilot.step.denied')
  const parts: string[] = [dictLabel(`pilot.step.${s.label}` as MessageKey)]
  if (s.status === 'running') {
    parts.push(t('pilot.step.running'))
    return parts.join(' · ')
  }
  if (s.status === 'stopped') {
    parts.push(t('pilot.step.stopped'))
    return parts.join(' · ')
  }
  if (s.status === 'timeout') {
    parts.push(t('pilot.step.timeout'))
  } else if (s.status === 'failed') {
    parts.push(t('pilot.step.failed'))
  } else if (s.status === 'empty') {
    parts.push(t('pilot.step.empty'))
  } else if (typeof s.data.count === 'number') {
    parts.push(t('pilot.step.hits', { count: s.data.count }))
  } else if (s.label === 'navigate' && s.data.target) {
    parts.push(navModuleName(s.data.target))
  } else if (s.data.name) {
    parts.push(t('pilot.step.target', { name: s.data.name }))
  }
  return parts.join(' · ')
}
</script>

<template>
  <div class="pchain" :class="{ 'is-open': open, 'is-live': live }">
    <button type="button" class="pchain__head" @click="toggle">
      <div class="pchain__indicator">
        <HlIcon v-if="live" name="zap" :size="13" />
        <span v-else-if="failed" class="pchain__fail-icon">
          <HlIcon name="close" :size="12" />
        </span>
        <span v-else class="pchain__done-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <polyline points="20 6 9 17 4 12" />
          </svg>
        </span>
      </div>
      <span class="pchain__label" :class="{ 'is-shimmer': live }">{{ headLabel }}</span>
      <svg class="pchain__chevron" :class="{ 'is-open': open }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <polyline points="9 6 15 12 9 18" />
      </svg>
    </button>

    <div v-if="open" class="pchain__body">
      <div
        v-for="(p, pi) in viewPhases"
        v-show="p.thinking || p.text || p.steps.length || phaseCards(pi).length || pi === activeIndex"
        :key="pi"
        class="pchain__phase"
      >
        <HlPilotThinking
          v-if="p.thinking || (pi === activeIndex && !(hidePhaseText && phasePre(p)))"
          :text="p.thinking"
          :live="pi === activeIndex"
          :active="pi === activeIndex"
          :dur-sec="pi === activeIndex ? null : phaseSec(p)"
          :initial-open="pi === activeIndex"
          :waiting-text="waitingText"
        />
        <p v-if="p.truncated" class="pchain__truncated">{{ t('pilot.think.truncated') }}</p>
        <p v-if="phasePre(p) && p.steps.length" class="pchain__pre">{{ phasePre(p) }}</p>

        <div class="pchain__steps-list">
          <div
            v-for="(s, si) in p.steps"
            :key="si"
            class="pilot-step"
            :class="`is-${s.status}`"
          >
            <div class="pilot-step__icon-box">
              <svg v-if="s.status === 'ok'" class="pilot-step__svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              <HlIcon v-else-if="s.status === 'running'" name="zap" :size="12" />
              <svg v-else-if="s.status === 'failed'" class="pilot-step__svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
              <svg v-else-if="s.status === 'denied'" class="pilot-step__svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
              <span v-else class="pilot-step__dot-subtle"></span>
            </div>
            <span class="pilot-step__text">{{ stepText(s) }}</span>
          </div>
        </div>

        <!-- 过程卡内嵌：思考时序原位（工具步骤下方），随后续思考/步骤上移；
             done 后由回答层的正式卡接管，这里只在流式期有值 -->
        <template v-if="phaseCards(pi).length">
          <div v-for="entry in phaseCards(pi)" :key="entry.key" class="pchain__card">
            <HlPilotCards
              v-if="entry.card"
              :cards="[entry.card]"
              :busy-pid="busyPid"
              @proposal="emit('proposal', $event)"
            />
            <div v-else class="pchain__card-skel" role="status" aria-busy="true">
              <HlSkeleton variant="title" />
              <HlSkeleton variant="text" :rows="3" />
            </div>
          </div>
        </template>
      </div>

      <div v-if="memoryText && !live" class="pchain__memory">
        <svg class="pchain__memory-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
        </svg>
        <span class="pchain__memory-text">{{ memoryText }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pchain {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  align-self: stretch;
}

.pchain__head {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-soft);
  background: var(--surface-panel);
  cursor: pointer;
  text-align: left;
  transition: all var(--transition);
}

.pchain__head:hover {
  background: var(--surface-chip);
  border-color: var(--border-strong);
}

.pchain.is-live .pchain__head {
  border-color: var(--accent-a30);
  background: var(--accent-a08);
}

.pchain__indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  color: var(--text-muted);
}

.pchain.is-live .pchain__indicator {
  color: var(--accent);
}

.pchain__fail-icon {
  width: 13px;
  height: 13px;
  color: var(--danger);
  display: flex;
  align-items: center;
  justify-content: center;
}

.pchain__done-icon {
  width: 13px;
  height: 13px;
  color: var(--success);
  display: flex;
  align-items: center;
  justify-content: center;
}

.pchain__done-icon svg {
  width: 12px;
  height: 12px;
}

.pchain__label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}

.pchain__label.is-shimmer {
  background: linear-gradient(90deg, var(--text-muted) 30%, var(--accent) 50%, var(--text-muted) 70%);
  background-size: 300% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: pchain-sweep 3s linear infinite;
}

@keyframes pchain-sweep {
  to {
    background-position: -300% 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .pchain__label.is-shimmer {
    animation: none;
    background: none;
    color: var(--accent);
  }
}

.pchain__chevron {
  width: 12px;
  height: 12px;
  color: var(--text-muted);
  flex: none;
  opacity: 0;
  transition: transform var(--transition), opacity var(--transition);
}

.pchain__head:hover .pchain__chevron,
.pchain.is-open .pchain__chevron {
  opacity: 1;
}

.pchain__chevron.is-open {
  transform: rotate(90deg);
}

.pchain__body {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  align-self: stretch;
  gap: 8px;
  margin-top: 8px;
  margin-left: 6px;
  padding-left: 14px;
  border-left: 2px solid var(--border-soft);
  max-width: 100%;
}

.pchain__phase {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  align-self: stretch;
  gap: 8px;
  max-width: 100%;
}

.pchain__pre {
  align-self: stretch;
  margin: 0;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface-panel);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  text-align: left;
}

.pchain__truncated {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-muted);
  text-align: left;
}

.pchain__steps-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-self: stretch;
}

.pchain__card {
  align-self: stretch;
  max-width: 100%;
}

.pchain__card-skel {
  align-self: stretch;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-soft);
  background: var(--surface-panel);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pilot-step {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 8px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  line-height: 1.4;
  color: var(--text-secondary);
  background: var(--surface-panel);
  border: 1px solid var(--border-soft);
  align-self: flex-start;
  max-width: 100%;
}

.pilot-step__icon-box {
  flex: none;
  width: 14px;
  height: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.pilot-step__svg {
  width: 12px;
  height: 12px;
}

.pilot-step.is-ok .pilot-step__icon-box {
  color: var(--success);
}

.pilot-step.is-running .pilot-step__icon-box {
  color: var(--accent);
}

.pilot-step.is-failed .pilot-step__icon-box {
  color: var(--danger);
}

.pilot-step.is-denied .pilot-step__icon-box {
  color: var(--warning);
}

.pilot-step__dot-subtle {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--text-faint);
}

.pilot-step__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pchain__memory {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-muted);
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
  margin-top: 2px;
}

.pchain__memory-icon {
  width: 13px;
  height: 13px;
  color: var(--text-faint);
  flex-shrink: 0;
}
</style>
