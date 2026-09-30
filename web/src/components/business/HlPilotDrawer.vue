<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import {
  askPilotStream,
  type PilotActionFacts,
  type PilotAskResponse,
  type PilotFacts,
  type PilotGameFactsItem,
  type PilotNavigateFacts,
  type PilotPriceFacts,
  type PilotStep,
} from '@/api/client'
import { formatCnyFen } from '@/api/regions'
import { useRouter } from 'vue-router'
import { useI18n, type MessageKey } from '@/locales'
import HlButton from '@/components/ui/HlButton.vue'
import HlDrawer from '@/components/ui/HlDrawer.vue'
import HlTextarea from '@/components/ui/HlTextarea.vue'

/**
 * 领航台：领航员的提问面板（顶栏 / 找游戏 / 游戏详情三处入口共开）。
 * 时间线形态：每轮问答 = 问句 + agent 过程时间线（tool_start/tool 事件逐帧
 * 驱动，done.steps 全量回填历史）+ 回答 + 结构化卡片；工具机器名不出用户面，
 * 步骤文案由 `pilot.step.{label}` 词条渲染。thinking 以折叠态保留在历史轮。
 */
const props = defineProps<{
  modelValue: boolean
  game?: { appid: number; name: string } | null
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
const { t } = useI18n()
const router = useRouter()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

interface TurnItem {
  name: string | null
  cnyFen: number | null
  discount: number | null
}
interface Turn {
  q: string
  kind: 'candidates' | 'action' | 'price' | 'games' | 'answer' | 'guide' | 'reason'
  text?: string
  reasonKey?: MessageKey
  items?: TurnItem[]
  followLink?: boolean
  cards?: PilotFacts[]
  cached?: boolean
  steps: PilotStep[]
  think?: string
  thinkOpen?: boolean
}

const question = ref('')
const streaming = ref(false)
const liveThinking = ref('')
const liveThinkOpen = ref(true)
const liveAnswer = ref('')
const liveSteps = ref<PilotStep[]>([])
const turns = ref<Turn[]>([])
// 会话 id：每次开舱重新生成——同一开舱内的追问（候选序数 / 指代）靠它串起
const sessionId = ref('')
const scrollBox = ref<HTMLDivElement | null>(null)

const REASON_KEYS: Record<string, MessageKey> = {
  llm_off: 'pilot.reason.llm_off',
  cap_reached: 'pilot.reason.cap_reached',
  llm_failed: 'pilot.reason.llm_failed',
  no_data: 'pilot.reason.no_data',
  need_target: 'pilot.reason.need_target',
}

watch(visible, (open) => {
  if (open) {
    turns.value = []
    sessionId.value =
      globalThis.crypto?.randomUUID?.() ?? `s-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  }
})

function fen(v: number | null | undefined): string {
  return typeof v === 'number' && v > 0 ? formatCnyFen(v) : '—'
}

function actionReceipt(f: PilotActionFacts): string {
  if (f.action === 'monitor_add') return t('pilot.action.monitor', { name: f.name ?? '—' })
  if (f.targetType === 'historic_low') return t('pilot.action.alertLow', { name: f.name ?? '—' })
  return t('pilot.action.alertPrice', { name: f.name ?? '—', price: fen(f.targetValueFen) })
}

function navModuleName(target: string): string {
  const key = `pilot.nav.${target}` as MessageKey
  return t(key)
}

/** 聚焦框：主内容区外圈亮环，2.2s 后淡出移除 */
function focusMainRegion() {
  const el = document.querySelector('.app-shell__main')
  if (!el) return
  const r = el.getBoundingClientRect()
  const ring = document.createElement('div')
  ring.style.cssText = [
    'position:fixed', `top:${r.top - 6}px`, `left:${r.left - 6}px`,
    `width:${r.width + 12}px`, `height:${r.height + 12}px`,
    'border:2px solid var(--accent)', 'border-radius:14px',
    'box-shadow:0 0 0 4px var(--accent-a15), 0 0 24px var(--accent-a15)',
    'pointer-events:none', 'z-index:60',
    'transition:opacity 0.5s', 'opacity:1',
  ].join(';')
  document.body.appendChild(ring)
  setTimeout(() => {
    ring.style.opacity = '0'
    setTimeout(() => ring.remove(), 600)
  }, 1800)
}

/** 导航卡生效：跳转模块 + 聚焦框（抽屉保持打开，模块在底层切换） */
function applyNavigate(f: PilotNavigateFacts) {
  if (!f.path) return
  void router.push(f.path)
  setTimeout(focusMainRegion, 700)
}

function priceBriefing(f: PilotPriceFacts): string {
  const base = {
    name: f.name ?? '—',
    price: fen(f.cn?.cnyFen),
    discount: f.cn?.discount ? t('pilot.facts.discount', { discount: f.cn.discount }) : '',
    lowest: f.lowest ? formatCnyFen(f.lowest.cnyFen) : t('pilot.facts.noLowest'),
  }
  if (!f.year) return t('pilot.facts.price_noyear', base)
  return t('pilot.facts.price', {
    ...base,
    ymin: formatCnyFen(f.year.minFen),
    ymax: formatCnyFen(f.year.maxFen),
    count: f.year.count,
  })
}

/** 步骤条目 → 用户语言一行：词条基础文案 + 最小插值（计数 / 对象名 / 模块名）。 */
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

function buildTurn(q: string, e: PilotAskResponse, liveStepSnapshot: PilotStep[]): Turn {
  const cards = (e.cards ?? []).filter(Boolean)
  const base = {
    q,
    steps: e.steps ?? liveStepSnapshot.map((s) => ({ ...s, data: { ...s.data } })),
    think: e.thinking || undefined,
    thinkOpen: false,
  }
  if (e.source === 'llm') return { q, kind: 'answer', text: e.answer || '', cards, ...base }
  if (e.source === 'facts') {
    if (cards.length) {
      const first = cards[0]
      if (first.kind === 'action') {
        return { q, kind: 'action', text: actionReceipt(first), followLink: first.action === 'monitor_add', cards, ...base }
      }
      if (first.kind === 'price') return { q, kind: 'price', text: priceBriefing(first), cards, ...base }
      return { q, kind: 'games', items: cardsGameItems(first), cards, ...base }
    }
    if (e.facts?.kind === 'action') {
      return { q, kind: 'action', text: actionReceipt(e.facts), followLink: e.facts.action === 'monitor_add', ...base }
    }
    if (e.facts?.kind === 'price') return { q, kind: 'price', text: priceBriefing(e.facts), ...base }
    if (e.facts?.kind === 'games') {
      return {
        q,
        kind: e.reason === 'need_target' ? 'candidates' : 'games',
        items: e.facts.items.map((it) => ({ name: it.name, cnyFen: it.cnyFen, discount: it.discount })),
        reasonKey: e.reason === 'need_target' ? 'pilot.reason.need_target' : undefined,
        ...base,
      }
    }
  }
  if (e.source === 'guide') return { q, kind: 'guide', ...base }
  return { q, kind: 'reason', reasonKey: (e.reason && REASON_KEYS[e.reason]) || 'pilot.error', ...base }
}

function cardsGameItems(f: PilotFacts): TurnItem[] {
  if (f.kind !== 'games') return []
  return f.items.map((it: PilotGameFactsItem) => ({
    name: it.name,
    cnyFen: it.cnyFen,
    discount: it.discount,
  }))
}

async function scrollBottom() {
  await nextTick()
  if (scrollBox.value) scrollBox.value.scrollTop = scrollBox.value.scrollHeight
}

async function ask(text?: string) {
  const q = (text ?? question.value).trim()
  if (!q || streaming.value) return
  if (!text) question.value = ''
  streaming.value = true
  liveThinking.value = ''
  liveThinkOpen.value = true
  liveAnswer.value = ''
  liveSteps.value = []
  let done: PilotAskResponse | null = null
  let failed = false
  try {
    await askPilotStream(q, props.game?.appid, sessionId.value || undefined, (e) => {
      if (e.type === 'thinking' && e.delta) {
        liveThinking.value += e.delta
      } else if (e.type === 'answer' && e.delta) {
        liveAnswer.value += e.delta
      } else if (e.type === 'tool_start') {
        liveSteps.value.push({ label: e.label ?? '', status: 'running', data: {} })
      } else if (e.type === 'tool') {
        // 回填最后一个运行态步骤；无运行态（如重放）直接追加
        const pending = [...liveSteps.value].reverse().find((s) => s.status === 'running')
        if (pending) {
          pending.label = e.label ?? pending.label
          pending.status = e.status ?? 'ok'
          pending.data = e.data ?? {}
        } else {
          liveSteps.value.push({ label: e.label ?? '', status: e.status ?? 'ok', data: e.data ?? {} })
        }
      } else if (e.type === 'done') {
        done = e as PilotAskResponse
      } else if (e.type === 'error') {
        done = { answer: '', thinking: null, source: 'none', reason: e.reason ?? 'llm_failed', facts: null, cards: [], cached: false }
      }
    })
  } catch {
    failed = true
  }
  const stepSnapshot = liveSteps.value.map((s) => ({ ...s, data: { ...s.data } }))
  const turn = failed
    ? { q, kind: 'reason', reasonKey: 'pilot.error', steps: stepSnapshot } as Turn
    : buildTurn(q, done ?? { answer: '', thinking: null, source: 'none', reason: 'llm_failed', facts: null, cards: [], cached: false }, stepSnapshot)
  turns.value.push(turn)
  // 导航卡生效：取本轮最后一张，跳模块并打聚焦框
  const navCards = (turn.cards ?? []).filter((c): c is PilotNavigateFacts => c.kind === 'navigate' && Boolean(c.path))
  const lastNav = navCards[navCards.length - 1]
  if (lastNav) applyNavigate(lastNav)
  streaming.value = false
  liveThinking.value = ''
  liveAnswer.value = ''
  liveSteps.value = []
  void scrollBottom()
}

function pickCandidate(turn: Turn, index: number) {
  if (turn !== turns.value[turns.value.length - 1] || streaming.value) return
  const name = turn.items?.[index]?.name
  // 回发候选名：后端名称检索唯中即执行；无名时按展示序号回发（词条化）
  if (name) {
    void ask(name)
    return
  }
  void ask(t('pilot.candidate.pick', { n: index + 1 }))
}
</script>

<template>
  <HlDrawer
    v-model="visible"
    :modal="false"
    :with-header="false"
    width="420px"
    :top="56"
    class="pilot-drawer"
  >
    <div class="pilot-console">
      <header class="pilot-head">
        <span class="pilot-head__title">{{ t('pilot.title') }} · {{ t('pilot.console') }}</span>
        <HlButton variant="text" @click="visible = false">{{ t('common.close') }}</HlButton>
      </header>

      <div v-if="game" class="pilot-context">
        <span class="pilot-context__label">{{ t('pilot.context.of') }}</span>
        <span class="pilot-context__name">{{ game.name }}</span>
      </div>

      <div ref="scrollBox" class="pilot-turns">
        <div v-for="(turn, i) in turns" :key="i" class="pilot-turn">
          <p class="pilot-turn__q">{{ turn.q }}</p>

          <!-- agent 过程时间线：done.steps 全量回填，历史轮保持可回看 -->
          <div v-if="turn.think || turn.steps.length" class="pilot-steps">
            <button
              v-if="turn.think"
              type="button"
              class="pilot-step pilot-step--toggle"
              :class="{ 'is-open': turn.thinkOpen }"
              @click="turn.thinkOpen = !turn.thinkOpen"
            >
              <span class="pilot-step__dot" aria-hidden="true"></span>
              <span class="pilot-step__text">{{ t('pilot.thinking') }}</span>
            </button>
            <p v-if="turn.think && turn.thinkOpen" class="pilot-think-body">{{ turn.think }}</p>
            <div
              v-for="(s, si) in turn.steps"
              :key="si"
              class="pilot-step"
              :class="`is-${s.status}`"
            >
              <span class="pilot-step__dot" aria-hidden="true"></span>
              <span class="pilot-step__text">{{ stepText(s) }}</span>
            </div>
          </div>

          <div v-if="turn.kind === 'reason'" class="pilot-reason">
            {{ turn.reasonKey ? t(turn.reasonKey) : '' }}
            <span v-if="turn.cached">({{ t('pilot.cached') }})</span>
          </div>

          <div v-else-if="turn.kind === 'candidates'" class="pilot-briefing">
            <div class="pilot-briefing__title">{{ turn.reasonKey ? t(turn.reasonKey) : '' }}</div>
            <button
              v-for="(g, gi) in turn.items"
              :key="gi"
              type="button"
              class="pilot-cand"
              :class="{ 'is-locked': i !== turns.length - 1 }"
              @click="pickCandidate(turn, gi)"
            >
              <span class="pilot-cand__name">{{ gi + 1 }}. {{ g.name }}</span>
              <span class="pilot-cand__meta">
                {{ t('pilot.candidate.price', { price: fen(g.cnyFen) }) }}<template v-if="g.discount"> · -{{ g.discount }}%</template>
              </span>
            </button>
          </div>

          <div v-else-if="turn.kind === 'action' && turn.text" class="pilot-briefing">
            <div class="pilot-briefing__title">{{ t('pilot.action.done') }}</div>
            <p class="pilot-briefing__text">
              {{ turn.text }}
              <router-link v-if="turn.followLink" class="pilot-guide__link" to="/pool">
                {{ t('pilot.action.toFollows') }}
              </router-link>
            </p>
          </div>

          <div v-else-if="turn.kind === 'price' && turn.text" class="pilot-briefing">
            <div class="pilot-briefing__title">{{ t('pilot.briefing') }}</div>
            <p class="pilot-briefing__text">{{ turn.text }}</p>
          </div>

          <div v-else-if="turn.kind === 'games' && turn.items" class="pilot-briefing">
            <div class="pilot-briefing__title">{{ t('pilot.facts.gamesTitle') }}</div>
            <ul class="pilot-games">
              <li v-for="(g, gi) in turn.items" :key="gi" class="pilot-games__item">
                <span class="pilot-games__name">{{ g.name }}</span>
                <span class="pilot-games__meta">
                  {{ fen(g.cnyFen) }}<template v-if="g.discount"> · -{{ g.discount }}%</template>
                </span>
              </li>
            </ul>
          </div>

          <p v-else-if="turn.kind === 'answer' && turn.text" class="pilot-answer">{{ turn.text }}</p>

          <div v-else-if="turn.kind === 'guide'" class="pilot-guide">
            <p class="pilot-guide__title">{{ t('pilot.guide.title') }}</p>
            <p>{{ t('pilot.guide.monitor') }}</p>
            <p>{{ t('pilot.guide.alert') }}</p>
            <router-link class="pilot-guide__link" to="/library">{{ t('pilot.guide.link') }}</router-link>
          </div>

          <!-- agent 轮的结构化卡片：回答之后渲染（组件复用数据层） -->
          <template v-if="turn.cards?.length">
            <div
              v-for="(c, ci) in turn.cards.filter((x) => x.kind === 'navigate')"
              :key="`n${ci}`"
              class="pilot-briefing"
            >
              <div class="pilot-briefing__title">{{ t('pilot.nav.doneTitle') }}</div>
              <p class="pilot-briefing__text">
                {{ t('pilot.nav.done', { module: navModuleName((c as PilotNavigateFacts).target) }) }}
              </p>
            </div>
          </template>
          <template v-if="turn.cards?.length && turn.kind === 'answer'">
            <div v-for="(c, ci) in turn.cards" :key="`c${ci}`" class="pilot-briefing">
              <template v-if="c.kind === 'games'">
                <div class="pilot-briefing__title">{{ t('pilot.facts.gamesTitle') }}</div>
                <router-link
                  v-for="g in c.items"
                  :key="g.appid"
                  class="pilot-cand"
                  :to="`/game/${g.appid}`"
                >
                  <span class="pilot-cand__name">{{ g.name }}</span>
                  <span class="pilot-cand__meta">
                    {{ fen(g.cnyFen) }}<template v-if="g.discount"> · -{{ g.discount }}%</template>
                  </span>
                </router-link>
              </template>
              <template v-else-if="c.kind === 'price'">
                <div class="pilot-briefing__title">{{ t('pilot.briefing') }}</div>
                <p class="pilot-briefing__text">{{ priceBriefing(c) }}</p>
              </template>
              <template v-else-if="c.kind === 'action'">
                <div class="pilot-briefing__title">{{ t('pilot.action.done') }}</div>
                <p class="pilot-briefing__text">
                  {{ actionReceipt(c) }}
                  <router-link
                    v-if="c.action === 'monitor_add'"
                    class="pilot-guide__link"
                    to="/pool"
                  >
                    {{ t('pilot.action.toFollows') }}
                  </router-link>
                </p>
              </template>
            </div>
          </template>
        </div>

        <div v-if="streaming" class="pilot-turn">
          <div v-if="liveThinking || liveSteps.length" class="pilot-steps is-live">
            <button
              v-if="liveThinking"
              type="button"
              class="pilot-step pilot-step--toggle"
              :class="{ 'is-open': liveThinkOpen }"
              @click="liveThinkOpen = !liveThinkOpen"
            >
              <span class="pilot-step__dot" aria-hidden="true"></span>
              <span class="pilot-step__text">{{ t('pilot.thinking.live') }}</span>
            </button>
            <p v-if="liveThinking && liveThinkOpen" class="pilot-think-body is-live">{{ liveThinking }}</p>
            <div
              v-for="(s, si) in liveSteps"
              :key="`l${si}`"
              class="pilot-step"
              :class="`is-${s.status}`"
            >
              <span class="pilot-step__dot" aria-hidden="true"></span>
              <span class="pilot-step__text">{{ stepText(s) }}</span>
            </div>
          </div>
          <p v-if="liveAnswer" class="pilot-answer">{{ liveAnswer }}</p>
          <div v-if="!liveThinking && !liveAnswer && !liveSteps.length" class="pilot-loading">
            {{ t('pilot.loading') }}
          </div>
        </div>
      </div>

      <HlTextarea
        v-model="question"
        :rows="2"
        :placeholder="t('pilot.ask.placeholder')"
        @keydown.enter.exact.prevent="ask()"
      />
      <div class="pilot-actions">
        <HlButton
          :loading="streaming"
          :disabled="!question.trim() || streaming"
          @click="ask()"
        >
          {{ t('pilot.ask.button') }}
        </HlButton>
      </div>
    </div>
  </HlDrawer>
</template>

<style scoped>
.pilot-console {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px 2px;
}

.pilot-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.pilot-head__title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.pilot-context {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--bg-card);
}

.pilot-context__label {
  font-size: 12px;
  color: var(--text-secondary);
}

.pilot-context__name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pilot-turns {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: 56vh;
  overflow-y: auto;
  padding-right: 4px;
}

.pilot-turn {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pilot-turn__q {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pilot-actions {
  display: flex;
  justify-content: flex-end;
}

.pilot-loading {
  padding: 16px 0;
  text-align: center;
  font-size: 13px;
  color: var(--text-secondary);
}

.pilot-reason {
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px dashed var(--border-soft);
}

.pilot-steps {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--bg-card);
}

.pilot-steps.is-live {
  border-style: dashed;
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

.pilot-step--toggle {
  width: 100%;
  padding: 0;
  border: none;
  background: transparent;
  cursor: pointer;
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

.pilot-step--toggle .pilot-step__dot::before {
  display: none;
}

.pilot-step--toggle .pilot-step__dot::after {
  content: '▸';
  font-size: 10px;
  color: var(--text-secondary);
  vertical-align: middle;
}

.pilot-step--toggle.is-open .pilot-step__dot::after {
  content: '▾';
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
  animation: pilot-dot-pulse calc(var(--duration-4) * var(--motion-scale)) ease-in-out infinite alternate;
}

@keyframes pilot-dot-pulse {
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

.pilot-think-body {
  margin: 0;
  padding: 2px 0 2px 20px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.pilot-answer {
  margin: 0;
  padding: 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
}

.pilot-briefing__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.pilot-briefing__text {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-primary);
}

.pilot-cand {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--bg-card);
  cursor: pointer;
  text-align: left;
}

.pilot-cand + .pilot-cand {
  margin-top: 6px;
}

.pilot-cand.is-locked {
  cursor: default;
  opacity: 0.6;
}

.pilot-cand__name {
  font-size: 13px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pilot-cand__meta {
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.pilot-games {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pilot-games__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
}

.pilot-games__name {
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pilot-games__meta {
  color: var(--text-secondary);
  white-space: nowrap;
}

.pilot-guide p {
  margin: 0 0 6px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-primary);
}

.pilot-guide__title {
  font-weight: 600;
}

.pilot-guide__link {
  font-size: 13px;
}
</style>
