<script setup lang="ts">
/**
 * 过程链折叠块：单轮回复的过程段收进一个链头一行摘要的折叠层，展开体内按
 * agent 循环步分阶段排列，每段依次是思考行、前言正文、工具步骤（无阶段记录
 * 的旧轮回落为单段形态）。开合规则状态驱动——运行中展开（秒表跳动），
 * 历史轮收起（链头即摘要），失败轮强制展开；用户手动开合后自动规则退位。
 * 链头时长取服务端整轮工时（elapsedSec），完成即冻结不再吃本地时钟。
 * 步骤文案由 `pilot.step.{label}` 词条渲染，工具机器名不出用户面。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import type { PilotPhase, PilotStep } from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import HlPilotThinking from '@/components/business/HlPilotThinking.vue'
import { fmtTokens } from '@/lib/pilotView'

const props = withDefaults(
  defineProps<{
    steps: PilotStep[]
    /** 按 agent 循环步分段的阶段记录（权威）；缺省时回落 think + steps 的单段形态 */
    phases?: PilotPhase[]
    think?: string
    thinkSec?: number | null
    /** 整轮工时（秒，服务端下发；缺省不显示时长） */
    elapsedSec?: number | null
    /** 记忆快照：本轮上下文占用 + 会话归档边界 + 缓存命中 */
    memory?: { tokens?: number | null; budget?: number | null; archived?: number | null; cached?: boolean } | null
    live?: boolean
    /** 首个 delta 前的等待话术（live 时透传给思考行） */
    waitingText?: string
    /** 失败/未完成轮：默认展开，完成后不自动收起 */
    failed?: boolean
  }>(),
  {
    phases: () => [],
    think: '',
    thinkSec: null,
    elapsedSec: null,
    memory: null,
    live: false,
    waitingText: '',
    failed: false,
  },
)

const { t } = useI18n()

/** 渲染用阶段列表：无 phases（旧会话 / 快捷动作轮）时把 think + steps 折成单段。 */
const viewPhases = computed<PilotPhase[]>(() => {
  if (props.phases.length) return props.phases
  if (props.think || props.steps.length || props.live) {
    return [{ step: 0, thinking: props.think, text: '', think_ms: props.thinkSec ? props.thinkSec * 1000 : null, steps: props.steps }]
  }
  return []
})

/** 当前阶段下标（流式期只有它展开思考体）；无阶段时为 -1 */
const activeIndex = computed(() => (props.live && viewPhases.value.length ? viewPhases.value.length - 1 : -1))

/** 已让位阶段的思考工时（秒）；无思考为 null（思考行不显示时长） */
function phaseSec(p: PilotPhase): number | null {
  return p.think_ms ? Math.max(1, Math.round(p.think_ms / 1000)) : null
}

/** 本轮工具步骤总数（跨阶段求和；链头摘要口径） */
const stepTotal = computed(() => viewPhases.value.reduce((n, p) => n + p.steps.length, 0))

const open = ref(props.live || props.failed)
const userTouched = ref(false)

function toggle() {
  userTouched.value = true
  open.value = !open.value
}

/* 流式→完成边界自动收起（摘要已在链头），手动开合过或失败轮则不介入 */
watch(
  () => props.live,
  (live, was) => {
    if (was && !live && !userTouched.value && !props.failed) open.value = false
  },
)

/* 运行秒表：仅 live 期间走表；完成态时长以服务端下发为准 */
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

/* 记忆快照一行：上下文占用 + 归档边界 + 缓存命中，全部缺省则整行不出 */
const memoryText = computed(() => {
  const m = props.memory
  if (!m) return ''
  const parts: string[] = []
  if (m.tokens && m.budget) parts.push(t('pilot.memory.ctx', { ctx: `${fmtTokens(m.tokens)}/${fmtTokens(m.budget)}` }))
  if (m.archived && m.archived > 0) parts.push(t('pilot.memory.archived', { n: m.archived }))
  if (m.cached) parts.push(t('pilot.memory.cachedHit'))
  return parts.join(' · ')
})

/** 步骤条目 → 用户语言一行：词条基础文案 + 最小插值（计数 / 对象名 / 模块名）。 */
function navModuleName(target: string): string {
  return t(`pilot.nav.${target}` as MessageKey)
}

/** 前言正文按纯文本展示（逐帧跑 markdown 代价过高），仅剥掉行内标记符，不露出源码。 */
function plainInline(s: string): string {
  return s.replace(/\*\*([^*]+)\*\*/g, '$1').replace(/==([^=]+)==/g, '$1').replace(/`([^`]+)`/g, '$1')
}

function stepText(s: PilotStep): string {
  if (s.status === 'denied') return t('pilot.step.denied')
  const parts: string[] = [t(`pilot.step.${s.label}` as MessageKey)]
  if (s.status === 'empty') {
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
      <span class="pchain__icon" aria-hidden="true"></span>
      <span class="pchain__label" :class="{ 'is-shimmer': live }">{{ headLabel }}</span>
      <span class="pchain__chevron" aria-hidden="true"></span>
    </button>
    <div v-if="open" class="pchain__body">
      <div
        v-for="(p, pi) in viewPhases"
        v-show="p.thinking || p.text || p.steps.length || pi === activeIndex"
        :key="pi"
        class="pchain__phase"
      >
        <HlPilotThinking
          v-if="p.thinking || pi === activeIndex"
          :text="p.thinking"
          :live="pi === activeIndex"
          :active="pi === activeIndex"
          :dur-sec="pi === activeIndex ? null : phaseSec(p)"
          :initial-open="pi === activeIndex"
          :waiting-text="waitingText"
        />
        <p v-if="p.truncated" class="pchain__truncated">{{ t('pilot.think.truncated') }}</p>
        <p v-if="p.text" class="pchain__pre">{{ plainInline(p.text) }}</p>
        <div
          v-for="(s, si) in p.steps"
          :key="si"
          class="pilot-step"
          :class="`is-${s.status}`"
        >
          <span class="pilot-step__dot" aria-hidden="true"></span>
          <span class="pilot-step__text">{{ stepText(s) }}</span>
        </div>
      </div>
      <div v-if="memoryText && !live" class="pchain__memory">
        <span class="pilot-step__dot" aria-hidden="true"></span>
        <span class="pilot-step__text">{{ memoryText }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pchain {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}

.pchain__head {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  padding: 2px 0;
  border: none;
  background: none;
  cursor: pointer;
  text-align: left;
}

.pchain__icon {
  flex-shrink: 0;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  border: 1.5px solid var(--text-faint);
}

.pchain.is-live .pchain__icon {
  border-color: var(--accent);
}

.pchain__label {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-muted);
}

/* 运行态流光：渐变裁切文字，替代旋转 loader（与思考行同语言） */
.pchain__label.is-shimmer {
  background: linear-gradient(90deg, var(--text-faint) 34%, var(--text-secondary) 50%, var(--text-faint) 66%);
  background-size: 300% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: pchain-sweep 4s linear infinite;
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
    color: var(--text-secondary);
  }
}

.pchain__chevron {
  flex-shrink: 0;
  width: 7px;
  height: 7px;
  border-right: 1.5px solid var(--text-faint);
  border-bottom: 1.5px solid var(--text-faint);
  transform: rotate(-45deg);
  opacity: 0;
  transition: transform 0.2s, opacity 0.2s;
}

.pchain__head:hover .pchain__chevron,
.pchain.is-open .pchain__chevron {
  opacity: 1;
}

.pchain.is-open .pchain__chevron {
  transform: rotate(45deg) translate(-2px, -2px);
}

/* 链体：左导线串起思考 / 工具 / 记忆三类条目。
   stretch 必须显式写——父级沿交叉轴 flex-start，不写就按内容收缩，
   正文会被压成贴着最长行的窄条，面板右侧大片留白。 */
.pchain__body {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  align-self: stretch;
  gap: 6px;
  margin-top: 6px;
  margin-left: 2px;
  padding-left: 12px;
  border-left: 1px solid var(--border-soft);
  max-width: 100%;
}

.pchain__memory {
  display: flex;
  align-items: center;
  align-self: stretch;
  gap: 8px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-secondary);
  text-align: left;
}

/* 阶段块：一步的思考 / 前言 / 工具自成一组，步骤行按序排在其下 */
.pchain__phase {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  align-self: stretch;
  gap: 6px;
  max-width: 100%;
}

/* 前言正文：该步正文随阶段就地展示，纯文本不跑 markdown（逐帧解析代价过高） */
.pchain__pre {
  align-self: stretch;
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
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

.pilot-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-secondary);
  text-align: left;
}

.pilot-step__dot {
  flex: none;
  width: 12px;
  text-align: center;
}

.pilot-step__dot::before {
  content: '';
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--success);
  vertical-align: middle;
}

.pilot-step.is-empty .pilot-step__dot::before {
  background: var(--text-secondary);
  opacity: 0.45;
}

.pilot-step.is-denied .pilot-step__dot::before {
  background: var(--warning);
}

.pilot-step.is-running .pilot-step__dot::before {
  background: var(--accent);
  animation: pchain-dot-pulse calc(var(--duration-4) * var(--motion-scale)) ease-in-out infinite alternate;
}

@keyframes pchain-dot-pulse {
  from {
    opacity: 1;
  }
  to {
    opacity: 0.35;
  }
}

@media (prefers-reduced-motion: reduce) {
  .pilot-step.is-running .pilot-step__dot::before {
    animation: none;
  }
}
</style>
