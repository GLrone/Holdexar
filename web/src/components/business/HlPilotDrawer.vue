<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

import {
  askPilotStream,
  type PilotActionFacts,
  type PilotAskResponse,
  type PilotFacts,
  type PilotGameFacts,
  type PilotPriceFacts,
} from '@/api/client'
import { formatCnyFen } from '@/api/regions'
import { useI18n, type MessageKey } from '@/locales'
import HlButton from '@/components/ui/HlButton.vue'
import HlDrawer from '@/components/ui/HlDrawer.vue'
import HlTextarea from '@/components/ui/HlTextarea.vue'

/**
 * 领航台：领航员的提问面板（找游戏页主入口 / 游戏详情页带对象入口）。
 * 多轮堆叠：每轮问答（问句 + 结果）保留在会话视图里可回看；
 * 候选追问轮的行可直接点选执行（同一会话内待定动作接管）。
 * thinking 通道 = 推理模型思维链，仅流式期间实时展示；
 * 回退原因 reason 是机器码，翻成用户语言在这层做。
 */
const props = defineProps<{
  modelValue: boolean
  game?: { appid: number; name: string } | null
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
const { t } = useI18n()

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
  cached?: boolean
}

const question = ref('')
const streaming = ref(false)
const liveThinking = ref('')
const liveAnswer = ref('')
const liveTools = ref<string[]>([])
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

function actionText(f: PilotActionFacts): string {
  if (f.action === 'monitor_add') return t('pilot.action.monitor', { name: f.name ?? '—' })
  if (f.targetType === 'historic_low') return t('pilot.action.alertLow', { name: f.name ?? '—' })
  return t('pilot.action.alertPrice', { name: f.name ?? '—', price: fen(f.targetValueFen) })
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

function buildTurn(q: string, e: PilotAskResponse): Turn {
  const f = e.facts ?? null
  if (e.source === 'llm') return { q, kind: 'answer', text: e.answer || '' }
  if (e.source === 'facts' && f?.kind === 'action') {
    return { q, kind: 'action', text: actionText(f), followLink: f.action === 'monitor_add' }
  }
  if (e.source === 'facts' && f?.kind === 'price') return { q, kind: 'price', text: priceBriefing(f) }
  if (e.source === 'facts' && f?.kind === 'games') {
    return {
      q,
      kind: e.reason === 'need_target' ? 'candidates' : 'games',
      items: f.items.map((it) => ({ name: it.name, cnyFen: it.cnyFen, discount: it.discount })),
      reasonKey: e.reason === 'need_target' ? 'pilot.reason.need_target' : undefined,
    }
  }
  if (e.source === 'guide') return { q, kind: 'guide' }
  return { q, kind: 'reason', reasonKey: (e.reason && REASON_KEYS[e.reason]) || 'pilot.error' }
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
  liveAnswer.value = ''
  let done: PilotAskResponse | null = null
  let failed = false
  try {
    await askPilotStream(q, props.game?.appid, sessionId.value || undefined, (e) => {
      if (e.type === 'thinking' && e.delta) {
        liveThinking.value += e.delta
      } else if (e.type === 'answer' && e.delta) {
        liveAnswer.value += e.delta
      } else if (e.type === 'tool' && e.summary) {
        liveTools.value.push(e.summary)
      } else if (e.type === 'done') {
        done = e as PilotAskResponse
      } else if (e.type === 'error') {
        done = { answer: '', thinking: null, source: 'none', reason: e.reason ?? 'llm_failed', facts: null, cached: false }
      }
    })
  } catch {
    failed = true
  }
  turns.value.push(
    failed
      ? { q, kind: 'reason', reasonKey: 'pilot.error' }
      : buildTurn(q, done ?? { answer: '', thinking: null, source: 'none', reason: 'llm_failed', facts: null, cached: false }),
  )
  streaming.value = false
  liveThinking.value = ''
  liveAnswer.value = ''
  liveTools.value = []
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

          <div v-else-if="turn.kind === 'games'" class="pilot-briefing">
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

          <div v-else-if="turn.kind === 'action'" class="pilot-briefing">
            <div class="pilot-briefing__title">{{ t('pilot.action.done') }}</div>
            <p class="pilot-briefing__text">
              {{ turn.text }}
              <router-link v-if="turn.followLink" class="pilot-guide__link" to="/pool">
                {{ t('pilot.action.toFollows') }}
              </router-link>
            </p>
          </div>

          <div v-else-if="turn.kind === 'price'" class="pilot-briefing">
            <div class="pilot-briefing__title">{{ t('pilot.briefing') }}</div>
            <p class="pilot-briefing__text">{{ turn.text }}</p>
          </div>

          <p v-else-if="turn.kind === 'answer'" class="pilot-answer">{{ turn.text }}</p>

          <div v-else-if="turn.kind === 'guide'" class="pilot-guide">
            <p class="pilot-guide__title">{{ t('pilot.guide.title') }}</p>
            <p>{{ t('pilot.guide.monitor') }}</p>
            <p>{{ t('pilot.guide.alert') }}</p>
            <router-link class="pilot-guide__link" to="/library">{{ t('pilot.guide.link') }}</router-link>
          </div>
        </div>

        <div v-if="streaming" class="pilot-turn">
          <div v-if="liveThinking" class="pilot-thinking is-live">
            <div class="pilot-thinking__head">{{ t('pilot.thinking.live') }}</div>
            <p class="pilot-thinking__text">{{ liveThinking }}</p>
          </div>
          <div v-for="(toolLine, ti) in liveTools" :key="ti" class="pilot-tool">{{ toolLine }}</div>
          <p v-if="liveAnswer" class="pilot-answer">{{ liveAnswer }}</p>
          <div v-if="!liveThinking && !liveAnswer && !liveTools.length" class="pilot-loading">
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

.pilot-thinking {
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--bg-card);
  overflow: hidden;
}

.pilot-thinking.is-live {
  border-style: dashed;
}

.pilot-thinking__head {
  display: block;
  width: 100%;
  text-align: left;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--text-secondary);
  background: transparent;
  border: none;
}

.pilot-thinking__text {
  margin: 0;
  padding: 0 12px 10px;
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

.pilot-tool {
  padding: 6px 12px;
  border-radius: 8px;
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
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
