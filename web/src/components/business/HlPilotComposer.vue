<template>
  <div class="pilot-composer">
    <!-- 引用区：一次性上下文（当前对象 / 跨会话引用 / 待发引用 / 待发附件 / 页面元素），顶置在输入文本上方，多枚自动换行 -->
    <div
      v-if="game || referenceSession || pendingQuotes.length || pendingAttachments.length || attachBusy || pickedElements.length"
      class="pilot-composer__refs"
    >
      <!-- 当前对象上下文：随页面而来，不可移除 -->
      <span v-if="game" class="pilot-ref" :title="game.name">
        <HlImg
          :src="pilotCoverUrl(game.appid)"
          :alt="game.name"
          loading="lazy"
          class="pilot-ref__cover"
        >
          <template #fallback>
            <span class="pilot-ref__cover-fallback">{{ (game.name || '').slice(0, 1) }}</span>
          </template>
        </HlImg>
        <span class="pilot-ref__label">{{ t('pilot.context.of') }} · {{ game.name }}</span>
      </span>
      <!-- 跨会话引用：+ 弹最近会话清单（排除当前），选中后下一轮提问携带其要点（一次性） -->
      <span v-if="referenceSession" class="pilot-ref" :title="referenceSession.title">
        <svg class="pilot-ref__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M4 6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3H9l-5 4V6z" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <span class="pilot-ref__label">{{ t('pilot.ref.chip', { title: referenceSession.title }) }}</span>
        <button type="button" class="pilot-ref__x" :aria-label="t('pilot.ref.remove')" @click="emit('remove-reference')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" stroke-linecap="round" />
          </svg>
        </button>
      </span>
      <!-- 待发引用：拖选后落成带编号的胶囊；编号由用户在正文里摆位置，这里显示同一编号 -->
      <span
        v-for="quote in pendingQuotes"
        :key="quote.id"
        class="pilot-ref pilot-ref--quote"
        tabindex="0"
      >
        <span class="pilot-ref__no">{{ quote.n }}</span>
        <span class="pilot-ref__label">{{ quote.title }}</span>
        <button
          type="button"
          class="pilot-ref__x"
          :aria-label="t('pilot.ref.remove')"
          @click.stop="removePendingQuote(quote.id)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" stroke-linecap="round" />
          </svg>
        </button>
        <span class="pilot-ref__quote-full">{{ quote.text }}</span>
      </span>
      <!-- 待发附件：同类收拢为一颗胶囊，悬停/点击弹清单（面板内单条删除，胶囊 ✕ 全清） -->
      <div
        v-if="pendingAttachments.length || attachBusy"
        class="pilot-ref is-group"
        :class="{ 'is-busy': !pendingAttachments.length }"
        @click="attGroupOpen = !attGroupOpen"
        @mouseenter="attGroupOpen = true"
        @mouseleave="attGroupOpen = false"
      >
        <svg class="pilot-ref__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <span class="pilot-ref__label">{{ attachGroupLabel }}</span>
        <button
          v-if="pendingAttachments.length"
          type="button"
          class="pilot-ref__x"
          :aria-label="t('pilot.ref.remove')"
          @click.stop="pendingAttachments = []"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" stroke-linecap="round" />
          </svg>
        </button>
        <Transition name="hl-pop">
          <div v-if="attGroupOpen && pendingAttachments.length" class="pilot-ref__panel">
            <div v-for="a in pendingAttachments" :key="a.id" class="pilot-ref__row" :title="a.name">
              <svg class="pilot-ref__row-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <div class="pilot-ref__row-main">
                <div class="pilot-ref__row-title">{{ a.name }}</div>
                <div class="pilot-ref__row-meta">{{ t('pilot.attach.meta', { chars: fmtChars(a.chars), lines: a.lines }) }}</div>
              </div>
              <button
                type="button"
                class="pilot-ref__row-x"
                :aria-label="t('pilot.ref.remove')"
                @click.stop="removePendingAttachment(a.id)"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" stroke-linecap="round" />
                </svg>
              </button>
            </div>
          </div>
        </Transition>
      </div>
      <!-- 页面元素引用：同类收拢为一颗胶囊，悬停/点击弹清单；选择模式内再点已选元素仍可取消 -->
      <div
        v-if="pickedElements.length"
        class="pilot-ref is-group"
        @click="elGroupOpen = !elGroupOpen"
        @mouseenter="elGroupOpen = true"
        @mouseleave="elGroupOpen = false"
      >
        <svg class="pilot-ref__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M3 3l7 19 2-8 8-2L3 3z" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <span class="pilot-ref__label">{{ elGroupLabel }}</span>
        <button type="button" class="pilot-ref__x" :aria-label="t('pilot.ref.remove')" @click.stop="clearPickedEls()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" stroke-linecap="round" />
          </svg>
        </button>
        <Transition name="hl-pop">
          <div v-if="elGroupOpen" class="pilot-ref__panel">
            <div v-for="el in pickedElements" :key="el.id" class="pilot-ref__row" :title="el.selector">
              <svg class="pilot-ref__row-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M3 3l7 19 2-8 8-2L3 3z" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <div class="pilot-ref__row-main">
                <div class="pilot-ref__row-title">{{ el.label || el.text }}</div>
                <div class="pilot-ref__row-meta">{{ el.tag }} · {{ el.selector }}</div>
              </div>
              <button
                type="button"
                class="pilot-ref__row-x"
                :aria-label="t('pilot.ref.remove')"
                @click.stop="removePickedEl(el.id)"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" stroke-linecap="round" />
                </svg>
              </button>
            </div>
          </div>
        </Transition>
      </div>
    </div>
    <HlTextarea
      ref="input"
      v-model="question"
      :rows="2"
      class="pilot-composer__input"
      :placeholder="t('pilot.ask.placeholder')"
      @keydown="onComposerKeydown"
      @paste="onComposerPaste"
    />
    <div class="pilot-composer__bar">
      <div class="pilot-composer__chips">
        <HlDropdown :items="menuItems" trigger="click" direction="up" @select="emit('menu-select', $event)">
          <HlChip tone="neutral" class="pilot-ref-add" :title="t('pilot.ref.add')">
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
            </svg>
          </HlChip>
        </HlDropdown>
        <!-- 页面元素选择：点击进入选择模式，再点已选元素可取消 -->
        <button
          type="button"
          class="pilot-el-pick"
          :class="{ 'is-active': elPicking }"
          :title="elPicking ? t('pilot.elPicker.cancel') : t('pilot.elPicker.start')"
          :aria-label="elPicking ? t('pilot.elPicker.cancel') : t('pilot.elPicker.start')"
          @click="elPicking ? cancelElPicking() : startElPicking({ pickedHint: t('pilot.elPicker.picked') })"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M3 3l7 19 2-8 8-2L3 3z" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <div
          class="pilot-ctxpick"
          @click.stop
          @mouseenter="ctxOpen = true"
          @mouseleave="ctxOpen = false"
        >
          <button
            type="button"
            class="pilot-ctxpick__btn"
            :class="{ 'is-warn': ctxPercent > 85 }"
            :title="ctxTitle"
            :aria-label="ctxTitle"
            @click="ctxOpen = !ctxOpen"
          >
            <svg class="pilot-ctxpick__ring" viewBox="0 0 20 20" aria-hidden="true">
              <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor"
                      stroke-width="2.5" opacity="0.25" />
              <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor"
                      stroke-width="2.5" stroke-linecap="round"
                      stroke-dasharray="50.265 50.265"
                      :stroke-dashoffset="50.265 * (1 - ctxPercent / 100)"
                      transform="rotate(-90 10 10)" />
            </svg>
            <span v-if="lastCtx" class="pilot-ctxpick__num">{{ `${fmtTokens(lastCtx.tokens)}/${fmtTokens(lastCtx.budget)}` }}</span>
          </button>
          <Transition name="hl-pop">
            <div v-if="ctxOpen" class="pilot-ctxpick__panel">
              <div class="pilot-ctxpick__head">
                <span>{{ t('pilot.ctx.title') }}</span>
                <span class="pilot-ctxpick__num">
                  {{ lastCtx ? `${fmtTokens(lastCtx.tokens)} / ${fmtTokens(lastCtx.budget)}` : t('pilot.ctx.empty') }}
                </span>
              </div>
              <div class="pilot-ctxpick__track">
                <div
                  class="pilot-ctxpick__fill"
                  :class="{ 'is-warn': ctxPercent > 85 }"
                  :style="{ width: ctxPercent + '%' }"
                >
                  <i
                    v-for="seg in ctxSegments"
                    :key="seg.source"
                    :class="`is-${seg.source}`"
                    :style="{ flexGrow: seg.share }"
                    aria-hidden="true"
                  ></i>
                </div>
              </div>
              <div v-if="ctxSegments.length" class="pilot-ctxpick__legend">
                <div v-for="seg in ctxSegments" :key="seg.source" class="pilot-ctxpick__row">
                  <i :class="`is-${seg.source}`" aria-hidden="true"></i>
                  <span class="pilot-ctxpick__src">{{ t(`pilot.ctx.src.${seg.source}` as MessageKey) }}</span>
                  <span class="pilot-ctxpick__pct">{{ fmtPct(seg.share) }}</span>
                </div>
              </div>
              <div
                v-if="typeof lastCtx?.hitRate === 'number'"
                class="pilot-ctxpick__cache"
                :class="{ 'is-low': !ctxCacheVisible }"
              >
                <span>{{ t('pilot.ctx.cacheHit') }}</span>
                <span class="pilot-ctxpick__pct">{{ fmtPct(lastCtx.hitRate) }}</span>
              </div>
              <!-- 本会话消耗：账本全轮折叠（用量药丸口径——分桶精确数，
                   部分命中永不显示 100%；provider 未报告的桶整行不出） -->
              <div v-if="statsTotal > 0 && sessionStats" class="pilot-ctxpick__usage">
                <div class="pilot-ctxpick__usage-head">
                  <span>{{ t('pilot.usage.title') }}</span>
                  <span class="pilot-ctxpick__pct">{{ fmtInt(statsTotal) }}</span>
                </div>
                <div v-if="statsCacheHit != null" class="pilot-ctxpick__urow">
                  <span>{{ t('pilot.usage.cacheHit') }}</span>
                  <span>{{ fmtHit(statsCacheHit) }}</span>
                </div>
                <div class="pilot-ctxpick__urow">
                  <span>{{ t('pilot.usage.uncached') }}</span>
                  <span>{{ fmtInt((sessionStats.cache_base_tokens ?? sessionStats.usage_in) - (sessionStats.cache_read_tokens ?? 0)) }}</span>
                </div>
                <div v-if="sessionStats.cache_read_tokens != null" class="pilot-ctxpick__urow">
                  <span>{{ t('pilot.usage.cacheRead') }}</span>
                  <span>{{ fmtInt(sessionStats.cache_read_tokens) }}</span>
                </div>
                <div v-if="sessionStats.cache_write_tokens != null" class="pilot-ctxpick__urow">
                  <span>{{ t('pilot.usage.cacheWrite') }}</span>
                  <span>{{ fmtInt(sessionStats.cache_write_tokens) }}</span>
                </div>
                <div class="pilot-ctxpick__urow">
                  <span>{{ t('pilot.usage.output') }}</span>
                  <span>{{ fmtInt(sessionStats.usage_out) }}</span>
                </div>
              </div>
            </div>
          </Transition>
        </div>
      </div>

      <div class="pilot-composer__right">
        <!-- 模型就地切换：ghost chip + 向上清单 + 底部管理入口 -->
        <div v-if="pilotReady" ref="modelPickRoot" class="pilot-modelpick" @click.stop>
          <button
            type="button"
            class="pilot-modelpick__chip"
            :class="{ 'is-open': modelMenuOpen, 'is-busy': switchingModel }"
            @click="openModelMenu()"
          >
            <span class="pilot-modelpick__name">{{ pilotModel || t('pilot.composer.modelOff') }}</span>
            <span class="pilot-modelpick__chevron" aria-hidden="true"></span>
          </button>
          <Transition name="hl-pop">
            <div v-if="modelMenuOpen" class="pilot-modelpick__menu">
              <div class="pilot-modelpick__list">
                <div v-for="g in menuGroups" :key="g.id" class="pilot-modelpick__group">
                  <button
                    type="button"
                    class="pilot-modelpick__group-head"
                    :class="{ 'is-open': openGroups.has(g.id) }"
                    @click="toggleGroup(g.id)"
                  >
                    <span class="pilot-modelpick__group-name" :title="g.name">{{ g.name }}</span>
                    <span
                      v-if="g.active"
                      class="pilot-modelpick__group-badge"
                    >{{ t('pilot.modelMenu.inUse') }}</span>
                    <span class="pilot-modelpick__group-chev" aria-hidden="true"></span>
                  </button>
                  <template v-if="openGroups.has(g.id)">
                    <button
                      v-for="m in g.models"
                      :key="m"
                      type="button"
                      class="pilot-modelpick__item"
                      :class="{ 'is-on': m === pilotModel && g.active }"
                      @click="pickModel(g.id, m)"
                    >
                      <span class="pilot-modelpick__item-name" :title="m">{{ m }}</span>
                      <span
                        v-if="m === pilotModel && g.active"
                        class="pilot-modelpick__check"
                        aria-hidden="true"
                      ></span>
                    </button>
                  </template>
                </div>
              </div>
              <div class="pilot-modelpick__foot">
                <button type="button" class="pilot-modelpick__manage" @click="goSettings">
                  {{ t('pilot.modelMenu.empty') }}
                </button>
              </div>
            </div>
          </Transition>
        </div>
        <!-- 配置尚未取回（null）时不渲染，避免把「还在取」显示成「未配置」 -->
        <HlChip v-else-if="pilotReady === false" tone="accent" @click="goSettings">{{ t('pilot.composer.modelOff') }}</HlChip>
        <button
          v-if="streaming"
          type="button"
          class="pilot-send pilot-send--stop"
          :title="t('pilot.stop')"
          :aria-label="t('pilot.stop')"
          @click="emit('stop')"
        >
          <span class="pilot-send__square" aria-hidden="true"></span>
        </button>
        <HlButton
          v-else
          variant="primary"
          size="sm"
          class="pilot-send"
          :disabled="!question.trim()"
          :title="t('pilot.ask.button')"
          :aria-label="t('pilot.ask.button')"
          @click="emit('send')"
        >
          <span class="pilot-send__arrow" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none">
              <path d="M12 5v14M5 12l7-7 7 7" stroke="currentColor" stroke-width="2.2"
                    stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </span>
        </HlButton>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch, type ComponentPublicInstance } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n, type MessageKey } from '@/locales'
import { message } from '@/components/ui'
import HlButton from '@/components/ui/HlButton.vue'
import HlChip from '@/components/ui/HlChip.vue'
import HlDropdown from '@/components/ui/HlDropdown.vue'
import HlImg from '@/components/ui/HlImg.vue'
import HlTextarea from '@/components/ui/HlTextarea.vue'
import { useElementPicker } from '@/composables/useElementPicker'
import { buildCtxSegments, shouldShowCacheHitRate } from '@/lib/ctxSegments'
import { fmtTokens, pilotCoverUrl, quoteTitleOf } from '@/lib/pilotView'
import { pilotApi, type PilotAttachmentRef, type PilotProvider, type PilotSessionStats } from '@/api/client'

/** + 菜单条目（快捷直达与跨会话引用由宿主组装，本组件只展示与转发） */
interface ComposerMenuItem {
  label: string
  value: string
  icon?: string
  divided?: boolean
}

const props = withDefaults(defineProps<{
  /** 当前对象上下文（游戏详情页随页而来） */
  game?: { appid: number; name: string } | null
  /** 跨会话引用（宿主权威态，随发送清空） */
  referenceSession?: { sid: string; title: string } | null
  streaming: boolean
  /** 附件上传在途（宿主 attachText 持有） */
  attachBusy?: boolean
  lastCtx: { tokens: number; budget: number; breakdown?: { source: string; chars: number }[] | null; hitRate?: number | null } | null
  sessionStats: PilotSessionStats | null
  statsTotal: number
  statsCacheHit: number | null
  menuItems: ComposerMenuItem[]
}>(), {
  game: null,
  referenceSession: null,
  attachBusy: false,
})

const emit = defineEmits<{
  (e: 'send'): void
  (e: 'stop'): void
  (e: 'menu-select', item: { value?: string }): void
  (e: 'attach', text: string, name: string): void
  (e: 'remove-reference'): void
}>()

const question = defineModel<string>({ default: '' })

const { t } = useI18n()
const router = useRouter()

/** 页面元素选择器：用户点击输入框中的按钮进入选择模式，点击页面元素采集引用 */
const {
  isPicking: elPicking,
  picked: pickedElements,
  startPicking: startElPicking,
  cancelPicking: cancelElPicking,
  removePicked: removePickedEl,
  clearPicked: clearPickedEls,
} = useElementPicker()

/* ── 引用：带编号（引用1 / 引用2），编号在句子里标位置 ── */
interface PendingQuote {
  id: string
  /** 稳定编号：取最小未占用正整数，删除后不回填重排（句中标号保持有效） */
  n: number
  text: string
  /** 展示标题：过长截断，完整内容留给悬停面板 */
  title: string
}

const pendingQuotes = ref<PendingQuote[]>([])

/** 引用在提问正文里的标号：文案走词条（中英各自的形态用户都读得懂），编号用于回取正文 */
function quoteMarker(n: number): string {
  return `\u3010${t('pilot.quote.markerPrefix')}${n}\u3011`
}

/** 新引用的编号：最小未占用正整数（删掉引用1后新建仍可复用 1，旧标号不受影响） */
function nextQuoteNo(): number {
  const used = new Set(pendingQuotes.value.map((q) => q.n))
  let n = 1
  while (used.has(n)) n += 1
  return n
}

function newQuoteId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `q-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function removePendingQuote(id: string) {
  pendingQuotes.value = pendingQuotes.value.filter((q) => q.id !== id)
}

/** 取回一颗待发引用并回填输入框光标处（消息面引用钮 / 拖选浮条经宿主转入） */
function insertQuoteAtCursor(text: string) {
  const body = text.trim()
  if (!body) return
  const n = nextQuoteNo()
  pendingQuotes.value = [...pendingQuotes.value, { id: newQuoteId(), n, text: body, title: quoteTitleOf(body) }]
  // 光标处插入标号：引用在句中的位置由用户自己摆，「引用1 不如 引用2」这种对比才写得出
  const el = input.value?.$el as HTMLTextAreaElement | undefined
  const cur = question.value
  const at = el?.selectionStart ?? cur.length
  const end = el?.selectionEnd ?? cur.length
  const marker = quoteMarker(n)
  question.value = `${cur.slice(0, at)}${marker}${cur.slice(end)}`
  const caret = at + marker.length
  void nextTick(() => {
    el?.focus()
    el?.setSelectionRange(caret, caret)
  })
}

const input = ref<ComponentPublicInstance | null>(null)

function onComposerKeydown(e: KeyboardEvent) {
  // Enter 发送；输入法组词确认的 Enter（isComposing）不是发送意图，放行给编辑器
  if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return
  if (e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return
  e.preventDefault()
  emit('send')
}

/* ── 长文本附件：粘贴超阈值即落盘，消息里只带引用（与后端 attachments 同源口径） ──
   内联上限 15360 字符（对齐后端常量）：超过就不进提问字段，改存附件文件，
   提问只带「名称 / 字符数 / 行数」；正文按需展开（区间读取，一次一块，可续读）。 */
const ATTACH_INLINE_LIMIT = 15360
/** 待发附件（发送后随本轮上屏并清空）；落盘由宿主承载（会话 id 权威在宿主） */
const pendingAttachments = ref<PilotAttachmentRef[]>([])

function fmtChars(n: number): string {
  return n >= 10000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

function removePendingAttachment(id: string) {
  pendingAttachments.value = pendingAttachments.value.filter((a) => a.id !== id)
}

/** 同类引用收拢胶囊：悬停/点击弹清单（面板内单条删除，胶囊 ✕ 全清）；条目清空即收起 */
const attGroupOpen = ref(false)
const elGroupOpen = ref(false)
watch(pendingAttachments, (v) => {
  if (!v.length) attGroupOpen.value = false
})
watch(pickedElements, (v) => {
  if (!v.length) elGroupOpen.value = false
})
const attachGroupLabel = computed(() => {
  const atts = pendingAttachments.value
  if (!atts.length) return t('pilot.attach.uploading')
  if (atts.length === 1) return t('pilot.attach.chip', { name: atts[0].name, n: atts[0].lines })
  return t('pilot.attach.group', { n: atts.length })
})
const elGroupLabel = computed(() => {
  const els = pickedElements.value
  if (els.length === 1) return t('pilot.elPicker.chip', { tag: els[0].tag, label: els[0].label || els[0].text })
  return t('pilot.elPicker.group', { n: els.length })
})

/** 粘贴拦截：短文本照常进输入框；长文本交宿主落盘换引用（长度不再是输入的天花板） */
function onComposerPaste(e: ClipboardEvent) {
  const text = e.clipboardData?.getData('text/plain') ?? ''
  if (!text || text.length <= ATTACH_INLINE_LIMIT) return
  e.preventDefault()
  const first = (text.split('\n')[0] ?? '').trim().slice(0, 24)
  emit('attach', text, first || t('pilot.attach.pastedName'))
}

/* ctx 消耗面板开合（数据由宿主随轮投递） */
const ctxOpen = ref(false)

/* ── 模型就地切换：ghost chip 弹向上清单，点击即切不关抽屉 ── */
const pilotModel = ref('')
const pilotReady = ref<boolean | null>(null)
const pilotProviders = ref<PilotProvider[]>([])
const pilotActiveId = ref('')
const switchingModel = ref(false)
const openGroups = ref<Set<string>>(new Set())

async function loadPilotStatus(retry = 0): Promise<void> {
  try {
    const cfg = await pilotApi.getConfig()
    pilotModel.value = cfg.model || ''
    pilotProviders.value = cfg.providers || []
    pilotActiveId.value = cfg.active || ''
    // 与后端 llm_ready 同口径：enabled + base_url + model；密钥可选，不参与判定
    pilotReady.value = Boolean(cfg.enabled && cfg.base_url && cfg.model)
  } catch {
    // 冷启动窗口（后端装配/DB 写锁排队）内首次拉取会失败：按退避重拉，
    // 不把一次失败当终态——否则整个会话都停在「未配置」，只有刷新页面才恢复
    if (retry < 4) {
      setTimeout(() => void loadPilotStatus(retry + 1), 800 * (retry + 1))
      return
    }
    pilotReady.value = false
  }
}

/** 分组清单：按供应商收敛，每组只出启用模型；当前模型不在启用清单也保底可见 */
const menuGroups = computed(() =>
  pilotProviders.value.map((p) => {
    const disabled = new Set(p.models_disabled)
    const models = p.models.filter((m) => !disabled.has(m))
    if (p.id === pilotActiveId.value && pilotModel.value && !models.includes(pilotModel.value)) {
      models.unshift(pilotModel.value)
    }
    return { id: p.id, name: p.name || t('pilot.title'), models, active: p.id === pilotActiveId.value }
  }),
)

function toggleGroup(id: string) {
  const next = new Set(openGroups.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  openGroups.value = next
}

const modelMenuOpen = ref(false)
const modelPickRoot = ref<HTMLElement | null>(null)

function openModelMenu() {
  const next = new Set(openGroups.value)
  if (pilotActiveId.value) next.add(pilotActiveId.value)
  openGroups.value = next
  modelMenuOpen.value = !modelMenuOpen.value
}

function onDocClick(e: MouseEvent) {
  if (modelPickRoot.value && !modelPickRoot.value.contains(e.target as Node)) modelMenuOpen.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  void loadPilotStatus()
})

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
})

async function pickModel(providerId: string, m: string) {
  const sameProvider = providerId === pilotActiveId.value
  if (switchingModel.value || (sameProvider && m === pilotModel.value)) {
    modelMenuOpen.value = false
    return
  }
  switchingModel.value = true
  try {
    await pilotApi.updateConfig(sameProvider ? { model: m } : { active: providerId, model: m })
    pilotActiveId.value = providerId
    pilotModel.value = m
    modelMenuOpen.value = false
  } catch {
    message.error(t('pilot.modelMenu.switchFailed'))
  }
  switchingModel.value = false
}

function goSettings() {
  void router.push('/settings')
}

/* ── ctx 消耗面板（数据由宿主随轮投递） ── */
const ctxSegments = computed(() => buildCtxSegments(props.lastCtx?.breakdown))
const ctxTitle = computed(() =>
  props.lastCtx
    ? t('pilot.composer.ctx', { n: `${fmtTokens(props.lastCtx.tokens)}/${fmtTokens(props.lastCtx.budget)}` })
    : t('pilot.ctx.empty'),
)
const ctxCacheVisible = computed(() => shouldShowCacheHitRate(props.lastCtx?.hitRate, import.meta.env.DEV))
const ctxPercent = computed(() => {
  if (!props.lastCtx || props.lastCtx.budget <= 0) return 0
  return Math.min(Math.round((props.lastCtx.tokens / props.lastCtx.budget) * 100), 100)
})

const statsNumFmt = computed(() => new Intl.NumberFormat(useI18n().locale.value === 'zh-CN' ? 'zh-CN' : 'en'))
function fmtInt(v: number): string {
  return statsNumFmt.value.format(v)
}
/** 命中率百分比：部分命中永不进位到 100（99.9x 诚实显示） */
function fmtHit(hit: number): string {
  const p = hit * 100
  if (p >= 100) return '100'
  if (p > 99.9) return '99.9'
  return String(Math.round(p * 10) / 10)
}
const pctFmt = new Intl.NumberFormat(useI18n().locale.value === 'zh-CN' ? 'zh-CN' : 'en', {
  style: 'percent',
  maximumFractionDigits: 1,
})
function fmtPct(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? pctFmt.format(v) : '—'
}

/** 发送载荷：待发引用（编号+正文）、附件清单、页面元素精简引用（只传 agent 需要的字段） */
function getComposerPayload(): {
  quotes: { n: number; text: string }[]
  attachments: PilotAttachmentRef[]
  elements: { tag: string; label: string; text: string; selector: string; role: string }[]
} {
  return {
    quotes: pendingQuotes.value.map((q) => ({ n: q.n, text: q.text })),
    attachments: [...pendingAttachments.value],
    elements: pickedElements.value.map((p) => ({
      tag: p.tag,
      label: p.label,
      text: p.text,
      selector: p.selector,
      role: p.role,
    })),
  }
}

/** 只清待发引用（换会话/新对话：草稿口径——附件与元素引用跨会话延续既有行为） */
function clearQuotes() {
  pendingQuotes.value = []
}

/** 发送后全清（附件已随轮上屏、元素引用一次性） */
function clearPending() {
  pendingQuotes.value = []
  pendingAttachments.value = []
  clearPickedEls()
}

/** 宿主落盘成功后回填一颗附件引用 */
function addAttachment(ref_: PilotAttachmentRef) {
  pendingAttachments.value = [...pendingAttachments.value, ref_]
}

/** 发送失败时放回整份待发附件（用户可一键重试，不重传内容） */
function restoreAttachments(list: PilotAttachmentRef[]) {
  pendingAttachments.value = list
}

defineExpose({ getComposerPayload, insertQuoteAtCursor, clearQuotes, clearPending, addAttachment, restoreAttachments })
</script>

<style scoped>
/* 输入区 = 唯一 accent 描边面（对话区无 accent 边框），聚焦时亮起光晕——
   与用户气泡/agent 面板三层互异，抽屉里一眼可辨「这是输入的地方」 */
.pilot-composer {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--accent-a30);
  border-radius: 12px;
  background: var(--bg-base);
  transition: border-color var(--duration-4) var(--motion-scale),
              box-shadow var(--duration-4) var(--motion-scale);
}
/* 加号引用入口：与右侧 chip 同语言的小方钮 */
.pilot-ref-add {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  padding: 0;
}
.pilot-ref-add svg {
  width: 14px;
  height: 14px;
}
/* 页面元素选择按钮：与 + 引用按钮同尺寸同风格，激活时高亮 */
.pilot-el-pick {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background calc(var(--duration-2) * var(--motion-scale)) ease,
    color calc(var(--duration-2) * var(--motion-scale)) ease;
}
.pilot-el-pick:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.pilot-el-pick.is-active {
  background: var(--accent-a20);
  color: var(--accent-a);
}
.pilot-el-pick svg {
  width: 14px;
  height: 14px;
}
.pilot-composer:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent-a15) inset, 0 0 18px var(--accent-a15);
}
.pilot-composer .pilot-composer__input {
  min-height: 0;
  padding: 2px 4px;
  background: transparent;
  border-radius: 0;
  box-shadow: none;
  resize: none;
}
/* 聚焦光晕由容器承载，textarea 自身的内发光关掉 */
.pilot-composer .pilot-composer__input:focus {
  box-shadow: none;
}
/* 圆形发送钮：复用 HlButton（primary 配色 + 内置 loading），这里只改尺寸 */
.pilot-send {
  flex: none;
  width: 34px;
  height: 34px;
  padding: 0;
  border-radius: 50%;
}
.pilot-send__arrow {
  display: grid;
  place-items: center;
}
.pilot-send__arrow svg {
  width: 16px;
  height: 16px;
}
.pilot-send svg {
  width: 16px;
  height: 16px;
}
.pilot-send--stop {
  display: grid;
  place-items: center;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
  color: var(--danger);
  cursor: pointer;
}
.pilot-send--stop:hover {
  border-color: var(--danger);
}
.pilot-send__square {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  background: currentColor;
}
/* 引用区：一次性上下文胶囊（当前对象 / 引用 / 附件 / 页面元素），删除钮 hover 显形 */
.pilot-composer__refs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.pilot-ref {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 3px 6px 3px 9px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: var(--bg-soft);
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 18px;
}
.pilot-ref__icon {
  width: 13px;
  height: 13px;
  flex: none;
  color: var(--text-muted);
}
.pilot-ref__label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 200px;
}
.pilot-ref__cover {
  width: 26px;
  aspect-ratio: 460 / 215;
  border-radius: var(--radius-xs, 2px);
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
  flex: none;
}
.pilot-ref__cover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.pilot-ref__cover-fallback {
  font-size: 9px;
  font-weight: 700;
  color: var(--text-muted);
}
.pilot-ref__x {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity calc(var(--duration-2) * var(--motion-scale)) ease,
    background calc(var(--duration-2) * var(--motion-scale)) ease,
    color calc(var(--duration-2) * var(--motion-scale)) ease;
}
.pilot-ref:hover .pilot-ref__x,
.pilot-ref__x:focus-visible {
  opacity: 1;
}
.pilot-ref__x:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.pilot-ref__x svg {
  width: 9px;
  height: 9px;
  display: block;
}
@media (hover: none) {
  .pilot-ref__x {
    opacity: 1;
  }
}
.pilot-ref.is-busy {
  padding: 3px 9px;
  border-style: dashed;
  background: transparent;
  color: var(--text-muted);
}
/* 收拢组：同类引用一颗胶囊，清单面板向上弹（同上下文环面板语言） */
.pilot-ref.is-group {
  position: relative;
  cursor: default;
}
.pilot-ref__panel {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 0;
  z-index: 7;
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 320px;
  max-height: 264px;
  overflow-y: auto;
  padding: 6px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
}
.pilot-ref__row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 26px 5px 8px;
  border-radius: 8px;
}
.pilot-ref__row:hover {
  background: var(--bg-hover);
}
.pilot-ref__row-icon {
  width: 12px;
  height: 12px;
  flex: none;
  color: var(--text-muted);
}
.pilot-ref__row-main {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.pilot-ref__row-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text-primary);
}
.pilot-ref__row-meta {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--text-muted);
}
.pilot-ref__row-x {
  position: absolute;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity calc(var(--duration-2) * var(--motion-scale)) ease,
    background calc(var(--duration-2) * var(--motion-scale)) ease,
    color calc(var(--duration-2) * var(--motion-scale)) ease;
}
.pilot-ref__row:hover .pilot-ref__row-x,
.pilot-ref__row-x:focus-visible {
  opacity: 1;
}
.pilot-ref__row-x:hover {
  background: var(--border-soft);
  color: var(--text-primary);
}
@media (hover: none) {
  .pilot-ref__row-x {
    opacity: 1;
  }
}
.pilot-ctxpick {
  position: relative;
  min-width: 0;
}
/* + 菜单向上弹出且不遮按键：bottom 锚在按键上方（同模型菜单），覆盖框架的 top+translate 技巧 */
.pilot-composer :deep(.hl-dropdown-pop.is-open-up) {
  top: auto;
  bottom: calc(100% + 6px);
  --pop-y: 0px;
}
.pilot-ctxpick__btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  font: inherit;
  font-size: 12px;
  color: var(--accent);
  border: none;
  border-radius: 999px;
  background: var(--accent-a10);
  cursor: pointer;
}
.pilot-ctxpick__btn:hover {
  color: var(--text-primary);
}
.pilot-ctxpick__btn.is-warn {
  color: var(--warning);
}
.pilot-ctxpick__ring {
  width: 14px;
  height: 14px;
}
.pilot-ctxpick__num {
  font-variant-numeric: tabular-nums;
}
.pilot-ctxpick__panel {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 0;
  z-index: 7;
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 260px;
  padding: 12px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
}
.pilot-ctxpick__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.pilot-ctxpick__num,
.pilot-ctxpick__pct {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  color: var(--text-primary);
}
.pilot-ctxpick__track {
  height: 6px;
  border-radius: 3px;
  background: var(--accent-a10);
  overflow: hidden;
}
.pilot-ctxpick__fill {
  display: flex;
  height: 100%;
  border-radius: 3px;
  overflow: hidden;
  min-width: 2px;
  transition: width calc(var(--duration-4, 0.2s) * var(--motion-scale, 1)) ease;
}
.pilot-ctxpick__fill.is-warn i {
  filter: saturate(0.6) brightness(0.9);
}
.pilot-ctxpick__fill i {
  display: block;
  min-width: 2px;
}
.pilot-ctxpick__fill i.is-system {
  background: var(--accent);
}
.pilot-ctxpick__fill i.is-tools {
  background: color-mix(in oklab, var(--accent) 78%, var(--bg-card));
}
.pilot-ctxpick__fill i.is-summary {
  background: color-mix(in oklab, var(--accent) 58%, var(--bg-card));
}
.pilot-ctxpick__fill i.is-history {
  background: color-mix(in oklab, var(--accent) 42%, var(--bg-card));
}
.pilot-ctxpick__fill i.is-current {
  background: color-mix(in oklab, var(--accent) 28%, var(--bg-card));
}
.pilot-ctxpick__legend {
  display: grid;
  gap: 4px;
}
.pilot-ctxpick__row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
.pilot-ctxpick__row i {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  border: 1px solid var(--border-soft);
}
.pilot-ctxpick__row i.is-system {
  background: var(--accent);
}
.pilot-ctxpick__row i.is-tools {
  background: color-mix(in oklab, var(--accent) 78%, var(--bg-card));
}
.pilot-ctxpick__row i.is-summary {
  background: color-mix(in oklab, var(--accent) 58%, var(--bg-card));
}
.pilot-ctxpick__row i.is-history {
  background: color-mix(in oklab, var(--accent) 42%, var(--bg-card));
}
.pilot-ctxpick__row i.is-current {
  background: color-mix(in oklab, var(--accent) 28%, var(--bg-card));
}
.pilot-ctxpick__src {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pilot-ctxpick__pct {
  margin-left: auto;
}
.pilot-ctxpick__cache {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
  font-size: 12px;
  color: var(--text-secondary);
}
.pilot-ctxpick__cache.is-low .pilot-ctxpick__pct {
  color: var(--text-secondary);
}
/* 本会话消耗段：段头（合计精确数）+ 分桶行，与图例行同字层 */
.pilot-ctxpick__usage {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
}
.pilot-ctxpick__usage-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 2px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}
.pilot-ctxpick__urow {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 0;
  font-size: 11px;
  color: var(--text-secondary);
}
.pilot-ctxpick__urow span:last-child {
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.pilot-composer__bar {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.pilot-composer__right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}
.pilot-composer__chips {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  /* 不能 overflow:hidden：+ 菜单与上下文面板都从这里向上弹出，剪掉=「点了没反应」 */
}
/* 待发引用胶囊的编号徽标：与发出去后的气泡内胶囊同一套编号视觉 */
.pilot-ref__no {
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
/* 待发引用胶囊：只露一行标题，完整引用悬停/聚焦才浮出 */
.pilot-ref--quote {
  position: relative;
  cursor: default;
  outline: none;
}
.pilot-ref__quote-full {
  position: absolute;
  right: 0;
  bottom: calc(100% + 6px);
  z-index: 7;
  display: none;
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
.pilot-ref--quote:hover .pilot-ref__quote-full,
.pilot-ref--quote:focus-visible .pilot-ref__quote-full {
  display: block;
}
.pilot-composer .hl-button {
  flex: none;
}
/* 模型就地切换：ghost chip + 向上弹清单 */
.pilot-modelpick {
  position: relative;
}
.pilot-modelpick__chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 220px;
  padding: 4px 8px;
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
}
.pilot-modelpick__chip:hover,
.pilot-modelpick__chip.is-open {
  background: var(--accent-a10);
}
.pilot-modelpick__chip.is-busy {
  opacity: 0.6;
  cursor: wait;
}
.pilot-modelpick__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}
.pilot-modelpick__chip.is-open .pilot-modelpick__name {
  color: var(--text-primary);
}
.pilot-modelpick__chevron {
  flex: none;
  width: 7px;
  height: 7px;
  border-right: 1.5px solid var(--text-faint);
  border-bottom: 1.5px solid var(--text-faint);
  transform: rotate(45deg) translateY(-1px);
  transition: transform 0.2s;
}
.pilot-modelpick__chip.is-open .pilot-modelpick__chevron {
  transform: rotate(-135deg) translateY(-2px);
}
.pilot-modelpick__menu {
  position: absolute;
  bottom: calc(100% + 8px);
  right: 0;
  z-index: 6;
  display: flex;
  flex-direction: column;
  min-width: 208px;
  max-width: min(300px, calc(100vw - 3rem));
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.pilot-modelpick__list {
  max-height: 288px;
  overflow-y: auto;
  padding: 4px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.pilot-modelpick__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  /* 按钮默认按内容收缩：不写 100% 时只有文字那一段可点，行内空白区点不动 */
  width: 100%;
  box-sizing: border-box;
  min-height: 32px;
  padding: 6px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
  text-align: left;
}
.pilot-modelpick__item:hover {
  background: var(--accent-a10);
}
.pilot-modelpick__item-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-family: var(--font-mono, monospace);
  color: var(--text-primary);
}
.pilot-modelpick__item.is-on .pilot-modelpick__item-name {
  color: var(--accent);
  font-weight: 600;
}
.pilot-modelpick__check {
  flex: none;
  width: 10px;
  height: 5px;
  border-left: 1.8px solid var(--accent);
  border-bottom: 1.8px solid var(--accent);
  transform: rotate(-45deg) translateY(-1px);
}
.pilot-modelpick__foot {
  border-top: 1px solid var(--border-soft);
  padding: 4px;
}
.pilot-modelpick__manage {
  width: 100%;
  padding: 6px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-secondary);
  text-align: left;
}
.pilot-modelpick__manage:hover {
  background: var(--accent-a10);
  color: var(--text-primary);
}
/* 菜单分组：厂商收敛头 + 组内模型行 */
.pilot-modelpick__group + .pilot-modelpick__group {
  border-top: 1px solid var(--border-soft);
}
.pilot-modelpick__group-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 7px 10px 5px;
  border: none;
  background: none;
  cursor: pointer;
  text-align: left;
}
.pilot-modelpick__group-head:hover {
  background: var(--accent-a10);
}
.pilot-modelpick__group-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
}
.pilot-modelpick__group-badge {
  flex: none;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--accent-a10);
  font-size: 10px;
  color: var(--accent);
}
.pilot-modelpick__group-chev {
  flex: none;
  width: 6px;
  height: 6px;
  border-right: 1.5px solid var(--text-faint);
  border-bottom: 1.5px solid var(--text-faint);
  transform: rotate(45deg);
}
.pilot-modelpick__group-head.is-open .pilot-modelpick__group-chev {
  transform: rotate(-135deg);
}
</style>
