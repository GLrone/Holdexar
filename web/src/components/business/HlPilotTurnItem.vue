<template>
  <div class="pilot-turn__row">
    <!-- 编辑态：改完提交 → 作废该轮及其后各轮，用新提问重跑 -->
    <div v-if="editing" class="pilot-edit">
      <textarea
        ref="editBox"
        v-model="editingText"
        class="pilot-edit__box"
        rows="2"
        :placeholder="t('pilot.edit.placeholder')"
        @keydown="onEditKeydown"
      ></textarea>
      <div class="pilot-edit__bar">
        <span v-if="editDropCount" class="pilot-edit__warn">
          {{ t('pilot.edit.dropWarn', { n: editDropCount }) }}
        </span>
        <button
          type="button"
          class="pilot-edit__btn"
          :disabled="editBusy || !editingText.trim()"
          @click="emit('edit-submit', index, editingText.trim())"
        >{{ t('pilot.edit.submit') }}</button>
        <button type="button" class="pilot-edit__btn is-ghost" @click="emit('edit-cancel')">
          {{ t('pilot.edit.cancel') }}
        </button>
      </div>
    </div>
    <!-- 提问气泡：文本段与助手回答同一 Markdown 契约（带标号时按行内渲染保胶囊句内位置，
         无标号整条走块级渲染）；复制/编辑重问仍取原始文本 -->
    <div v-else class="pilot-turn__q">
      <template v-for="(seg, si) in quoteSegments(turn.q)" :key="si">
        <span
          v-if="seg.kind === 'quote'"
          class="pilot-qchip"
          :class="{ 'is-missing': !quoteTextOf(turn, seg.n!) }"
          tabindex="0"
        >
          <span class="pilot-qchip__no">{{ seg.n }}</span>
          <span class="pilot-qchip__title">{{ quoteTextOf(turn, seg.n!) ? quoteTitleOf(quoteTextOf(turn, seg.n!)) : t('pilot.quote.gone') }}</span>
          <span v-if="quoteTextOf(turn, seg.n!)" class="pilot-qchip__full">{{ quoteTextOf(turn, seg.n!) }}</span>
        </span>
        <HlPilotMarkdown
          v-else-if="seg.value"
          :inline="qInline(turn)"
          :source="seg.value"
        />
      </template>
    </div>
  </div>

  <!-- 发言动作行：挂在气泡下方独立一行（右沿对齐），hover 才显形；
       动作不再挤在气泡旁边，气泡宽度与位置恒定 -->
  <div v-if="!editing" class="pilot-acts pilot-acts--end">
    <button
      type="button"
      class="pilot-act"
      :title="t('pilot.copy')"
      :aria-label="t('pilot.copy')"
      @click="emit('copy', turn.q)"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="9" y="9" width="11" height="11" rx="2" />
        <path d="M15 5H6a2 2 0 0 0-2 2v9" />
      </svg>
    </button>
    <!-- 改一句重问：已落账轮或失败/中断轮均可改（修改后重发） -->
    <button
      v-if="canEditTurn(turn)"
      type="button"
      class="pilot-act"
      :title="t('pilot.edit.title')"
      :aria-label="t('pilot.edit.title')"
      @click="emit('edit-start')"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3Z" />
        <path d="M14.5 6.5 17.5 9.5" />
      </svg>
    </button>
  </div>

  <!-- 本轮附件：长文本的引用（名称 / 字符数 / 行数），点开按需取块看内容。
       与回复层同级挂纵向的 pilot-turn 下——气泡那一行是横向 flex，
       留在行内会被当成行内第三项横排到气泡旁边。 -->
  <div v-if="turn.attachments?.length" class="pilot-atts">
    <button
      v-for="a in turn.attachments"
      :key="a.id"
      type="button"
      class="pilot-att"
      :class="{ 'is-open': openAtt?.ref.id === a.id }"
      :title="t('pilot.attach.toggle')"
      @click="emit('toggle-attachment', a)"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M14 3v5h5" />
        <path d="M15 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-4-5Z" />
      </svg>
      <span class="pilot-att__name">{{ a.name }}</span>
      <span class="pilot-att__meta">{{ t('pilot.attach.meta', { chars: fmtChars(a.chars), lines: a.lines }) }}</span>
    </button>
  </div>
  <div
    v-if="openAtt && turn.attachments?.some((a) => a.id === openAtt?.ref.id)"
    class="pilot-att-panel"
  >
    <pre class="pilot-att-panel__body">{{ openAtt.busy && !openAtt.chunk ? t('pilot.attach.loading') : openAtt.chunk?.content }}</pre>
    <div class="pilot-att-panel__bar">
      <span class="pilot-att-panel__note">{{ openAtt.chunk?.note }}</span>
      <button
        v-if="openAtt.chunk?.truncated"
        type="button"
        class="pilot-att-panel__more"
        :disabled="openAtt.busy"
        @click="emit('more-attachment', openAtt.ref)"
      >{{ t('pilot.attach.more') }}</button>
    </div>
  </div>

  <!-- agent 回复层：与用户气泡两种图层的统一左对齐面板；pending 轮只上屏发言，回复侧由下方 live 区承担 -->
  <div v-if="turn.kind !== 'pending'" class="pilot-reply">
    <div class="pilot-reply__header">
      <span class="pilot-reply__avatar">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="10" />
          <polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76" />
        </svg>
      </span>
      <span class="pilot-reply__role">Pilot</span>
      <!-- 中断续跑：已完成的工具事实回灌，接着把这一轮做完 -->
      <button
        v-if="turn.stopped && !streaming"
        type="button"
        class="pilot-resume"
        :title="t('pilot.continue.title')"
        @click="emit('continue-stopped', index)"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M6 3.5 20 12 6 20.5v-17Z" />
        </svg>
        <span>{{ t('pilot.continue') }}</span>
      </button>
    </div>

    <!-- agent 过程链：链头一行摘要（工时+步数），展开看思考/步骤/记忆；done.steps 全量回填 -->
    <HlPilotChain
      v-if="turn.phases?.length || turn.think || turn.steps.length"
      hide-phase-text
      :steps="turn.steps"
      :phases="turn.phases ?? []"
      :think="turn.think"
      :think-sec="turn.thinkSec ?? null"
      :elapsed-sec="turn.elapsedSec ?? null"
      :memory="turn.mem ?? null"
      :failed="turn.kind === 'reason'"
      @quote="emit('quote', $event)"
      @copy="emit('copy', $event)"
    />

    <!-- 结果异常/原因气泡：红色条件气泡支持行内直接重试 -->
    <div
      v-if="turn.kind === 'reason'"
      class="pilot-reason"
      :class="{ 'is-error': isErrorReason(turn.reasonKey) }"
    >
      <div class="pilot-reason__body">
        <svg
          v-if="isErrorReason(turn.reasonKey)"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          class="pilot-reason__icon"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="8" x2="12" y2="12" />
          <line x1="12" y1="16" x2="12.01" y2="16" />
        </svg>
        <span class="pilot-reason__text">
          {{ turn.reasonKey ? t(turn.reasonKey) : '' }}
          <span v-if="turn.cached">({{ t('pilot.cached') }})</span>
        </span>
      </div>
      <button
        v-if="canRetryTurn(turn)"
        type="button"
        class="pilot-reason__retry-btn"
        :disabled="streaming"
        :title="t('pilot.retry')"
        @click="emit('retry', index)"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="pilot-reason__retry-icon" aria-hidden="true">
          <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
          <path d="M3 3v5h5" />
          <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" />
          <path d="M16 21h5v-5" />
        </svg>
        <span>{{ t('pilot.retry') }}</span>
      </button>
    </div>

    <!-- 上游原文：挂在原因气泡下方，先读得懂的结论再给可展开的原文 -->
    <details v-if="turn.diagnostic" class="pilot-error-diagnostic" open>
      <summary>{{ t('pilot.error.details') }}</summary>
      <p>{{ turn.diagnostic.message || t('pilot.error.genericDetail') }}</p>
      <span>{{ t('pilot.error.diagnosticId', { id: turn.diagnostic.id }) }}</span>
    </details>

    <div v-else-if="turn.kind === 'candidates'" class="pilot-briefing">
      <div class="pilot-briefing__title">{{ turn.reasonKey ? t(turn.reasonKey) : '' }}</div>
      <div class="pilot-cands-grid">
        <button
          v-for="(g, gi) in turn.items"
          :key="gi"
          type="button"
          class="pilot-cand"
          :class="{ 'is-locked': !isLast }"
          @click="emit('candidate-pick', index, gi)"
        >
          <span class="pilot-cand__idx">{{ gi + 1 }}</span>
          <HlImg
            :src="pilotCoverUrl(g.appid)"
            :alt="g.name || `AppID ${g.appid}`"
            loading="lazy"
            class="pilot-cand__cover"
          >
            <template #fallback>
              <span class="pilot-cand__cover-fallback">{{ (g.name || `AppID ${g.appid}`).slice(0, 2) }}</span>
            </template>
          </HlImg>
          <div class="pilot-cand__main">
            <span class="pilot-cand__name" :title="g.name">{{ g.name }}</span>
            <div class="pilot-cand__meta">
              <span
                v-if="typeof g.positiveRate === 'number' && g.positiveRate > 0"
                class="pilot-cand__rating"
                :class="ratingClass(g.positiveRate)"
              >{{ positivePct(g.positiveRate) }}%</span>
              <span class="pilot-cand__price">{{ fen(g.cnyFen) }}</span>
              <span v-if="g.discount" class="pilot-cand__disc">-{{ g.discount }}%</span>
            </div>
          </div>
        </button>
      </div>
    </div>

    <HlPilotCards v-else-if="turn.kind === 'action'" :cards="turn.cards ?? []" actions
      @quote="emit('quote', $event)" @copy="emit('copy', $event)" />

    <HlPilotCards v-else-if="turn.kind === 'price'" :cards="turn.cards ?? []" actions
      @quote="emit('quote', $event)" @copy="emit('copy', $event)" />

    <HlPilotCards v-else-if="turn.kind === 'games'" :cards="turn.cards ?? []" actions
      @quote="emit('quote', $event)" @copy="emit('copy', $event)" />

    <!-- 批量提议：清单 + 确认/取消，确认后才真正执行 -->
    <div v-else-if="turn.kind === 'proposal'" class="pilot-proposal">
      <HlPilotCards
        :cards="turn.cards ?? []"
        actions
        @proposal="emit('proposal', $event)"
        @quote="emit('quote', $event)"
        @copy="emit('copy', $event)"
      />
      <HlPilotMarkdown
        v-if="turn.text"
        class="pilot-answer"
        :class="{ 'is-clamped': isLongAnswer(turn) && !turn.expanded }"
        :source="turn.text"
      />
      <button
        v-if="isLongAnswer(turn)"
        type="button"
        class="pilot-answer__more"
        @click="emit('toggle-answer', turn)"
      >{{ t(turn.expanded ? 'pilot.answer.collapse' : 'pilot.answer.expand') }}</button>
    </div>

    <!-- 回答：数据卡先行、终答文字收尾——与流式期布局同序，done 不重排 -->
    <template v-else-if="turn.kind === 'answer'">
      <HlPilotCards
        v-if="turn.cards?.length"
        :cards="turn.cards"
        actions
        @proposal="emit('proposal', $event)"
        @quote="emit('quote', $event)"
        @copy="emit('copy', $event)"
      />
      <HlPilotMarkdown
        v-if="turn.text"
        class="pilot-answer"
        :class="{ 'is-clamped': isLongAnswer(turn) && !turn.expanded }"
        :source="turn.text"
      />
      <button
        v-if="isLongAnswer(turn)"
        type="button"
        class="pilot-answer__more"
        @click="emit('toggle-answer', turn)"
      >{{ t(turn.expanded ? 'pilot.answer.collapse' : 'pilot.answer.expand') }}</button>
      <div v-if="turn.stopped" class="pilot-stopped">{{ t('pilot.stopped') }}</div>
    </template>

    <div v-else-if="turn.kind === 'guide'" class="pilot-guide">
      <p class="pilot-guide__title">{{ t('pilot.guide.title') }}</p>
      <p>{{ t('pilot.guide.monitor') }}</p>
      <p>{{ t('pilot.guide.alert') }}</p>
      <router-link class="pilot-guide__link" to="/library">{{ t('pilot.guide.link') }}</router-link>
    </div>

    <!-- 回复动作行：挂在回复正文下方独立一行（左沿对齐），hover 才显形。
         动作属于它作用的那条回复——不挤在身份标旁边，也不上到提问那一行 -->
    <div v-if="canRateTurn(turn) || canForkTurn(turn)" class="pilot-acts">
      <button
        v-if="turn.text"
        type="button"
        class="pilot-act"
        :title="t('pilot.copy')"
        :aria-label="t('pilot.copy')"
        @click="onCopyReply"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="9" y="9" width="11" height="11" rx="2" />
          <path d="M15 5H6a2 2 0 0 0-2 2v9" />
        </svg>
      </button>
      <button
        v-if="canRateTurn(turn)"
        type="button"
        class="pilot-act pilot-feedback"
        :class="{ 'is-selected': turn.feedback === 'accepted' }"
        :title="t('pilot.feedback.accepted')"
        :aria-label="t('pilot.feedback.accepted')"
        :aria-pressed="turn.feedback === 'accepted'"
        :disabled="feedbackBusy"
        @click="emit('rate', turn, 'accepted')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M7 10v11H4V10h3Zm3 11h7.1a2 2 0 0 0 1.96-1.6l1.5-7.5A2 2 0 0 0 18.6 9H14l.7-3.5A2 2 0 0 0 12.75 3L8 10.2V21Z" />
        </svg>
      </button>
      <button
        v-if="canRateTurn(turn)"
        type="button"
        class="pilot-act pilot-feedback"
        :class="{ 'is-rejected': turn.feedback === 'rejected' }"
        :title="t('pilot.feedback.rejected')"
        :aria-label="t('pilot.feedback.rejected')"
        :aria-pressed="turn.feedback === 'rejected'"
        :disabled="feedbackBusy"
        @click="emit('rate', turn, 'rejected')"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M17 14V3h3v11h-3Zm-3-11H6.9a2 2 0 0 0-1.96 1.6l-1.5 7.5A2 2 0 0 0 5.4 15H10l-.7 3.5A2 2 0 0 0 11.25 21L16 13.8V3Z" />
        </svg>
      </button>
      <!-- 话题分叉：从这一轮（含）派生新会话，源会话保留（同走账本记录） -->
      <button
        v-if="canForkTurn(turn)"
        type="button"
        class="pilot-act"
        :title="t('pilot.session.forkTitle')"
        :aria-label="t('pilot.session.fork')"
        @click="emit('fork', index)"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="6.5" cy="6" r="2.4" />
          <circle cx="6.5" cy="18" r="2.4" />
          <circle cx="17.5" cy="9.5" r="2.4" />
          <path d="M6.5 8.4v7.2M8.9 6h4.1a4.5 4.5 0 0 1 4.5 1.4" />
        </svg>
      </button>
    </div>

  </div>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useI18n } from '@/locales'
import HlImg from '@/components/ui/HlImg.vue'
import HlPilotCards from '@/components/business/HlPilotCards.vue'
import HlPilotChain from '@/components/business/HlPilotChain.vue'
import HlPilotMarkdown from '@/components/business/HlPilotMarkdown.vue'
import { formatCnyFen } from '@/api/regions'
import { canRateTurn, isErrorReason, fmtChars, isLongAnswerText, pilotCoverUrl, positivePct, quoteTitleOf, ratingClass, type Turn } from '@/lib/pilotView'

const props = withDefaults(defineProps<{
  turn: Turn
  index: number
  /** 候选锁：只有最后一轮可点选（历史候选仅回看） */
  isLast: boolean
  streaming: boolean
  /** 该轮是否处于编辑态（editingIndex 由宿主持有，同一时刻最多一轮） */
  editing: boolean
  editBusy: boolean
  /** 编辑重问将作废的轮数（需全清单口径，宿主计算） */
  editDropCount: number
  feedbackBusy: boolean
  /** 附件单开展开态（跨轮唯一，宿主持有） */
  openAtt: { ref: PilotAttachmentRef; chunk: PilotAttachmentChunk | null; busy: boolean } | null
}>(), {
  editBusy: false,
  openAtt: null,
})

const emit = defineEmits<{
  (e: 'copy', text: string): void
  (e: 'quote', text: string): void
  (e: 'edit-start'): void
  (e: 'edit-submit', index: number, text: string): void
  (e: 'edit-cancel'): void
  (e: 'retry', index: number): void
  (e: 'rate', turn: Turn, feedback: 'accepted' | 'rejected'): void
  (e: 'fork', index: number): void
  (e: 'candidate-pick', index: number, itemIndex: number): void
  (e: 'proposal', card: PilotFacts): void
  (e: 'continue-stopped', index: number): void
  (e: 'toggle-answer', turn: Turn): void
  (e: 'toggle-attachment', ref: PilotAttachmentRef): void
  (e: 'more-attachment', ref: PilotAttachmentRef): void
}>()

const { t } = useI18n()

/* 编辑态：文本框与丢弃计数在条目内自持；提交/取消语义事件交宿主执行 */
const editingText = ref('')
const editBox = ref<HTMLTextAreaElement | null>(null)
watch(() => props.editing, (on) => {
  if (on) {
    editingText.value = props.turn.q
    void nextTick(() => editBox.value?.focus())
  } else {
    editingText.value = ''
  }
})

function onEditKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    emit('edit-submit', props.index, editingText.value.trim())
  } else if (e.key === 'Escape') {
    emit('edit-cancel')
  }
}

function canEditTurn(turn: Turn): boolean {
  if (props.streaming) return false
  return typeof turn.turnIndex === 'number' || turn.kind === 'reason' || Boolean(turn.stopped)
}

function canForkTurn(turn: Turn): boolean {
  return typeof turn.turnIndex === 'number' && !props.streaming
}

function canRetryTurn(turn: Turn): boolean {
  return turn.kind === 'reason' && Boolean(turn.q)
}

function isLongAnswer(turn: Turn): boolean {
  return isLongAnswerText(turn.text)
}

/** 答复复制：取当面那条回复的可见正文（事件相对就近查 DOM，无正文的轮不挂钮） */
function onCopyReply(e: MouseEvent) {
  const body = (e.currentTarget as HTMLElement)
    .closest('.pilot-reply')
    ?.querySelector<HTMLElement>('.pilot-answer')
  emit('copy', body?.innerText ?? '')
}

/* ── 引用：带编号（引用1 / 引用2），编号在句子里标位置；
   待发清单与标号生成在 HlPilotComposer，这里只做气泡内解析回显 ── */
const QUOTE_MARKER_RE = /\u3010[^\u3011\d]*(\d+)\u3011/g

/** 提问正文按引用标号切片：文本段与引用段交替，渲染层据此把标号换成胶囊 */
function quoteSegments(q: string): { kind: 'text' | 'quote'; value: string; n?: number }[] {
  const out: { kind: 'text' | 'quote'; value: string; n?: number }[] = []
  const text = q || ''
  let last = 0
  QUOTE_MARKER_RE.lastIndex = 0
  for (const m of text.matchAll(QUOTE_MARKER_RE)) {
    const at = m.index ?? 0
    if (at > last) out.push({ kind: 'text', value: text.slice(last, at) })
    out.push({ kind: 'quote', value: m[0], n: Number(m[1]) })
    last = at + m[0].length
  }
  if (last < text.length) out.push({ kind: 'text', value: text.slice(last) })
  return out
}

/** 某编号引用的正文（气泡悬停面板按编号取） */
function quoteTextOf(turn: { quotes?: { n: number; text: string }[] }, n: number): string {
  return turn.quotes?.find((q) => q.n === n)?.text ?? ''
}

/** 提问正文段是否行内渲染：带编号引用的消息片段走行内契约（标号胶囊保持句内位置） */
function qInline(turn: { q: string }): boolean {
  QUOTE_MARKER_RE.lastIndex = 0
  return QUOTE_MARKER_RE.test(turn.q || '')
}

function fen(v: number | null | undefined): string {
  return typeof v === 'number' && v > 0 ? formatCnyFen(v) : '—'
}
</script>

<style scoped>
.pilot-stopped {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-secondary);
}
/* agent 回复统一图层：去盒子化，通透流式布局与 Agent 身份标 */
.pilot-reply {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 4px 0 10px;
  background: transparent;
  border: none;
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
}
.pilot-reply__header {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
  align-self: flex-start;
}
.pilot-reply__avatar {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
  color: var(--accent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.pilot-reply__avatar svg {
  width: 13px;
  height: 13px;
}
.pilot-reply__avatar.is-live {
  background: var(--accent-a15);
  border-color: var(--accent);
  animation: pavatar-pulse 1.8s ease-in-out infinite alternate;
}
.pilot-reply__role {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}
/* 发言行：气泡 + 复制钮同一行右沿对齐；钮常驻占位，hover 才显形 */
.pilot-turn__row {
  display: flex;
  align-items: flex-end;
  justify-content: flex-end;
  gap: 6px;
  min-width: 0;
}
.pilot-turn__q {
  align-self: flex-end;
  max-width: 82%;
  margin: 0;
  padding: 10px 16px;
  background: linear-gradient(135deg, var(--accent-a20), var(--accent-a10));
  border: 1px solid var(--accent-a30);
  border-radius: 18px 18px 4px 18px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: 0 2px 8px var(--surface-inset-sm);
}
/* ── 发出去的提问气泡内：编号引用就地渲染成胶囊（位置即用户在句中摆的位置）── */
.pilot-qchip {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
  margin: 0 2px;
  padding: 1px 7px 1px 3px;
  border: 1px solid var(--accent-a30);
  border-radius: 999px;
  background: var(--surface-panel);
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 16px;
  vertical-align: baseline;
  cursor: default;
  outline: none;
}
.pilot-qchip.is-missing {
  border-style: dashed;
  color: var(--text-muted);
}
.pilot-qchip__no {
  flex: none;
  display: inline-grid;
  place-items: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--accent-a20);
  color: var(--accent);
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
}
.pilot-qchip__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 180px;
}
/* 完整引用：只在悬停/聚焦时浮出，默认不占位 */
.pilot-qchip__full {
  position: absolute;
  left: 0;
  bottom: calc(100% + 6px);
  z-index: 8;
  display: none;
  width: max-content;
  max-width: min(420px, 78vw);
  padding: 8px 10px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-sm);
  background: var(--bg-card);
  box-shadow: var(--shadow-lg);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  text-align: left;
}
.pilot-qchip:hover .pilot-qchip__full,
.pilot-qchip:focus-visible .pilot-qchip__full {
  display: block;
}
/* ── 附件：引用芯片 + 展开的内容面板（长文本不进消息体，按需取块） ── */
.pilot-atts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
  align-self: flex-end;
  max-width: 82%;
  margin-top: 6px;
}
.pilot-att {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 100%;
  padding: 3px 9px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: var(--bg-soft);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}
.pilot-att:hover,
.pilot-att.is-open {
  color: var(--text-primary);
  border-color: var(--border-strong);
}
.pilot-att svg {
  width: 12px;
  height: 12px;
  flex: none;
  display: block;
}
.pilot-att__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 150px;
}
.pilot-att__meta {
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}
.pilot-att-panel {
  align-self: stretch;
  margin-top: 6px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-sm);
  background: var(--bg-soft);
  overflow: hidden;
}
.pilot-att-panel__body {
  margin: 0;
  padding: 8px 10px;
  max-height: 220px;
  overflow: auto;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
}
.pilot-att-panel__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 8px;
  border-top: 1px solid var(--border-soft);
}
.pilot-att-panel__note {
  font-size: 11px;
  color: var(--text-muted);
}
.pilot-att-panel__more {
  flex: none;
  padding: 2px 8px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.pilot-att-panel__more:disabled {
  opacity: 0.5;
  cursor: default;
}
.pilot-acts--end {
  justify-content: flex-end;
}
/* 发言动作：鼠标落在提问那一行或动作行自身时显形 */
.pilot-turn__row:hover ~ .pilot-acts--end,
.pilot-acts--end:hover,
.pilot-acts--end:focus-within {
  opacity: 1;
}
/* 回复动作：鼠标落在回复层或动作行自身时显形 */
.pilot-reply:hover .pilot-acts,
.pilot-reply .pilot-acts:hover,
.pilot-reply .pilot-acts:focus-within {
  opacity: 1;
}
/* 单枚动作钮：复制 / 编辑 / 分叉共用同一形态 */
.pilot-act {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 4px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.pilot-act svg {
  width: 13px;
  height: 13px;
  display: block;
}
.pilot-act:hover {
  background: var(--surface-chip);
  color: var(--text-primary);
}
.pilot-act:focus-visible {
  outline: 1px solid var(--accent);
  outline-offset: 1px;
}
.pilot-feedback.is-selected {
  color: var(--success);
}
.pilot-feedback.is-rejected {
  color: var(--danger-deep);
}
.pilot-feedback:disabled {
  cursor: default;
  opacity: 0.5;
}
/* 编辑重问：气泡原位换成可改的文本框，其后轮次会被作废（有提示） */
.pilot-edit {
  flex: 1;
  min-width: 0;
  max-width: 82%;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  background: var(--bg-card);
  border: 1px solid var(--accent-a40);
  border-radius: 16px 16px 4px 16px;
}
.pilot-edit__box {
  width: 100%;
  min-height: 50px;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.6;
  resize: vertical;
  outline: none;
}
.pilot-edit__bar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pilot-edit__warn {
  flex: 1;
  min-width: 0;
  font-size: 11px;
  color: var(--danger-deep);
}
.pilot-edit__btn {
  flex: none;
  padding: 4px 11px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: var(--accent-fill);
  color: var(--text-on-fill);
  font-size: 12px;
  cursor: pointer;
}
.pilot-edit__btn.is-ghost {
  background: transparent;
  border-color: var(--border-soft);
  color: var(--text-secondary);
}
.pilot-edit__btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.pilot-edit__btn:hover:not(:disabled) {
  background: var(--accent-fill-hover);
}
.pilot-edit__btn.is-ghost:hover:not(:disabled) {
  background: var(--surface-chip);
  color: var(--text-primary);
}
/* 中断续跑：答复头上的「继续」，只有被用户停止过的轮才有 */
.pilot-resume {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 2px;
  padding: 3px 9px;
  border: 1px solid var(--accent-a30);
  border-radius: 999px;
  background: var(--accent-a10);
  color: var(--accent-deep);
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.pilot-resume svg {
  width: 11px;
  height: 11px;
  display: block;
}
.pilot-resume:hover {
  background: var(--accent-a20);
  border-color: var(--accent-a50);
}
.pilot-reason {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px dashed var(--border-soft);
}
.pilot-reason.is-error {
  color: var(--danger);
  background: var(--danger-a15);
  border: 1px solid var(--danger-a30);
}
.pilot-reason__body {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}
.pilot-reason__icon {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  color: var(--danger);
}
.pilot-reason__text {
  flex: 1;
  word-break: break-word;
}
.pilot-error-diagnostic {
  margin: 6px 0 8px 23px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.pilot-error-diagnostic summary {
  width: fit-content;
  color: var(--danger);
  cursor: pointer;
}
.pilot-error-diagnostic p {
  margin: 5px 0 2px;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.pilot-reason__retry-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  font-size: 12px;
  font-weight: 500;
  border-radius: 6px;
  border: 1px solid var(--danger-a40);
  background: var(--danger-a20);
  color: var(--danger);
  cursor: pointer;
  flex-shrink: 0;
  transition: all var(--duration-fast) var(--ease-out);
}
.pilot-reason__retry-btn:hover:not(:disabled) {
  background: var(--danger);
  color: var(--text-on-fill);
  border-color: var(--danger);
}
.pilot-reason__retry-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.pilot-reason__retry-icon {
  width: 13px;
  height: 13px;
}
.pilot-briefing__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.pilot-cands-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 8px;
  margin-top: 8px;
}
.pilot-cand {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-sm);
  background: var(--surface-card);
  cursor: pointer;
  text-align: left;
  transition: all var(--duration-2) ease;
}
.pilot-cand:hover:not(.is-locked) {
  border-color: var(--accent);
  background: var(--accent-a08);
  transform: translateY(-1px);
  box-shadow: var(--shadow-sm);
}
.pilot-cand.is-locked {
  cursor: default;
  opacity: 0.65;
}
.pilot-cand__idx {
  flex: none;
  width: 16px;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-faint);
  text-align: center;
}
.pilot-cand__cover {
  flex: none;
  width: 68px;
  aspect-ratio: 460 / 215;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
}
.pilot-cand__cover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.pilot-cand__cover-fallback {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted);
}
.pilot-cand__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.pilot-cand__name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pilot-cand__meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  white-space: nowrap;
}
.pilot-cand__price {
  font-weight: 600;
  color: var(--text-primary);
}
.pilot-cand__disc {
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  background: var(--success);
  color: var(--ink-on-fill);
  font-size: 10px;
  font-weight: 700;
  line-height: 1.2;
}
.pilot-cand__rating {
  font-size: 11px;
  font-weight: 600;
  color: var(--success);
}
.pilot-cand__rating.medium {
  color: var(--warning);
}
.pilot-cand__rating.low {
  color: var(--text-muted);
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
