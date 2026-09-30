<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  askPilotStream,
  type PilotAskResponse,
  type PilotActionFacts,
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
 * 走流式问答：thinking 通道 = 推理模型思维链（实时展示、结束后可折叠），
 * answer 通道 = 回答正文；facts = 航报事实摘要；回退原因 reason 是机器码，
 * 翻成用户语言在这层做。
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

type Phase = 'input' | 'streaming' | 'done' | 'failed'

const question = ref('')
const phase = ref<Phase>('input')
const thinking = ref('')
const answer = ref('')
const showThinking = ref(true)
const finalSource = ref<PilotAskResponse['source'] | null>(null)
const finalReason = ref<string | null>(null)
const finalFacts = ref<PilotFacts | null>(null)
const cachedFlag = ref(false)

const REASON_KEYS: Record<string, MessageKey> = {
  llm_off: 'pilot.reason.llm_off',
  cap_reached: 'pilot.reason.cap_reached',
  llm_failed: 'pilot.reason.llm_failed',
  no_data: 'pilot.reason.no_data',
  need_target: 'pilot.reason.need_target',
}

async function ask() {
  const q = question.value.trim()
  if (!q || phase.value === 'streaming') return
  phase.value = 'streaming'
  thinking.value = ''
  answer.value = ''
  finalSource.value = null
  finalReason.value = null
  finalFacts.value = null
  cachedFlag.value = false
  showThinking.value = true
  try {
    await askPilotStream(q, props.game?.appid, (e) => {
      if (e.type === 'thinking' && e.delta) {
        thinking.value += e.delta
      } else if (e.type === 'answer' && e.delta) {
        answer.value += e.delta
      } else if (e.type === 'facts' && e.facts) {
        finalFacts.value = e.facts
      } else if (e.type === 'done') {
        finalSource.value = e.source ?? null
        finalReason.value = e.reason ?? null
        if (e.facts) finalFacts.value = e.facts
        if (e.answer) answer.value = e.answer
        if (e.thinking) thinking.value = e.thinking
        cachedFlag.value = Boolean(e.cached)
        phase.value = 'done'
        showThinking.value = false
      } else if (e.type === 'error') {
        finalReason.value = e.reason ?? 'llm_failed'
        phase.value = 'done'
      }
    })
    if (phase.value === 'streaming') phase.value = 'done'
  } catch {
    phase.value = 'failed'
  }
}

function fen(v: number | null | undefined): string {
  return typeof v === 'number' && v > 0 ? formatCnyFen(v) : '—'
}

const priceFacts = computed(() =>
  finalFacts.value?.kind === 'price' ? (finalFacts.value as PilotPriceFacts) : null,
)
const gameFacts = computed(() =>
  finalFacts.value?.kind === 'games' ? (finalFacts.value as PilotGameFacts) : null,
)
const actionFacts = computed(() =>
  finalFacts.value?.kind === 'action' ? (finalFacts.value as PilotActionFacts) : null,
)

const actionText = computed(() => {
  const f = actionFacts.value
  if (!f) return ''
  if (f.action === 'monitor_add') return t('pilot.action.monitor', { name: f.name ?? '—' })
  if (f.targetType === 'historic_low') return t('pilot.action.alertLow', { name: f.name ?? '—' })
  return t('pilot.action.alertPrice', { name: f.name ?? '—', price: fen(f.targetValueFen) })
})

const briefing = computed(() => {
  const f = priceFacts.value
  if (!f) return ''
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
})
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

      <HlTextarea
        v-model="question"
        :rows="2"
        :placeholder="t('pilot.ask.placeholder')"
        @keydown.enter.exact.prevent="ask"
      />
      <div class="pilot-actions">
        <HlButton
          :loading="phase === 'streaming'"
          :disabled="!question.trim() || phase === 'streaming'"
          @click="ask"
        >
          {{ t('pilot.ask.button') }}
        </HlButton>
      </div>

      <div v-if="phase === 'streaming'" class="pilot-live">
        <div v-if="thinking" class="pilot-thinking is-live">
          <div class="pilot-thinking__head">{{ t('pilot.thinking.live') }}</div>
          <p class="pilot-thinking__text">{{ thinking }}</p>
        </div>
        <p v-if="answer" class="pilot-answer">{{ answer }}</p>
        <div v-if="!thinking && !answer" class="pilot-loading">{{ t('pilot.loading') }}</div>
      </div>

      <template v-else-if="phase === 'done'">
        <div v-if="finalReason && REASON_KEYS[finalReason]" class="pilot-reason">
          {{ t(REASON_KEYS[finalReason]) }}
          <span v-if="cachedFlag">({{ t('pilot.cached') }})</span>
        </div>

        <template v-if="finalSource === 'llm'">
          <div v-if="thinking" class="pilot-thinking">
            <button
              type="button"
              class="pilot-thinking__head pilot-thinking__toggle"
              @click="showThinking = !showThinking"
            >
              {{ t('pilot.thinking') }} {{ showThinking ? '▾' : '▸' }}
            </button>
            <p v-show="showThinking" class="pilot-thinking__text">{{ thinking }}</p>
          </div>
          <p v-if="answer" class="pilot-answer">{{ answer }}</p>
        </template>

        <div v-else-if="finalSource === 'facts' && actionFacts" class="pilot-briefing">
          <div class="pilot-briefing__title">{{ t('pilot.briefing') }}</div>
          <p class="pilot-briefing__text">{{ actionText }}</p>
        </div>

        <div v-else-if="finalSource === 'facts'" class="pilot-briefing">
          <div class="pilot-briefing__title">{{ t('pilot.briefing') }}</div>
          <p v-if="priceFacts" class="pilot-briefing__text">{{ briefing }}</p>
          <template v-else-if="gameFacts">
            <p class="pilot-briefing__text">{{ t('pilot.facts.gamesTitle') }}</p>
            <ul class="pilot-games">
              <li v-for="g in gameFacts.items" :key="g.appid" class="pilot-games__item">
                <span class="pilot-games__name">{{ g.name }}</span>
                <span class="pilot-games__meta">
                  {{ fen(g.cnyFen) }}<template v-if="g.discount"> · -{{ g.discount }}%</template>
                </span>
              </li>
            </ul>
          </template>
        </div>

        <div v-else-if="finalSource === 'guide'" class="pilot-guide">
          <p class="pilot-guide__title">{{ t('pilot.guide.title') }}</p>
          <p>{{ t('pilot.guide.monitor') }}</p>
          <p>{{ t('pilot.guide.alert') }}</p>
          <router-link class="pilot-guide__link" to="/library">{{ t('pilot.guide.link') }}</router-link>
        </div>
      </template>

      <div v-else-if="phase === 'failed'" class="pilot-reason">{{ t('pilot.error') }}</div>
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
  cursor: default;
}

.pilot-thinking__toggle {
  cursor: pointer;
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
