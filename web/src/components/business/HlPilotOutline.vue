<script setup lang="ts">
/**
 * 对话目录：当前会话每轮「问一行 + 答摘要」的弹出清单，点击滚动定位该轮。
 * turns 的只读投影（压缩分隔线不入目录），不持账本；当前轮下标由宿主按
 * 滚动位置采样下发，本组件只负责展示与外点关闭。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { useI18n, type MessageKey } from '@/locales'

interface OutlineTurn {
  q: string
  kind: string
  text?: string
  reasonKey?: MessageKey
  items?: { name: string | null }[]
  cards?: { kind: string; name?: string | null }[]
}

const props = defineProps<{
  turns: OutlineTurn[]
  activeIndex: number | null
  /** 面板弹出方位：down=按钮下方（tabbar 场景），right=按钮右侧（会话列场景） */
  side?: 'down' | 'right'
}>()
const emit = defineEmits<{
  (e: 'jump', index: number): void
  (e: 'toggle', open: boolean): void
}>()

const { t } = useI18n()

/* 摘要预算与面板行 clamp 对齐：问题一行、回复两行 */
const Q_LIMIT = 80
const PREVIEW_LIMIT = 160

function clip(s: string, limit: number): string {
  const normalized = s.replace(/\s+/g, ' ').trim()
  return normalized.length > limit ? `${normalized.slice(0, limit - 1).trimEnd()}…` : normalized
}

function stripMd(s: string): string {
  return s
    .replace(/```[\s\S]*?(```|$)/g, ' ')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*-{3,}\s*$/gm, ' ')
    .replace(/\*\*|==|`/g, '')
}

function previewOf(turn: OutlineTurn): string {
  if (turn.kind === 'answer') return turn.text ? clip(stripMd(turn.text), PREVIEW_LIMIT) : ''
  if (turn.kind === 'games') return turn.items?.length ? t('pilot.outline.kindGames', { n: turn.items.length }) : ''
  if (turn.kind === 'candidates') return turn.reasonKey ? t(turn.reasonKey) : ''
  if (turn.kind === 'price') return turn.cards?.[0]?.name || t('pilot.outline.kindPrice')
  if (turn.kind === 'action') return turn.cards?.[0]?.name || t('pilot.outline.kindAction')
  if (turn.kind === 'proposal') return turn.text ? clip(stripMd(turn.text), PREVIEW_LIMIT) : t('pilot.outline.kindProposal')
  if (turn.kind === 'guide') return t('pilot.guide.title')
  if (turn.kind === 'reason') return turn.reasonKey ? t(turn.reasonKey) : ''
  return ''
}

interface OutlineEntry {
  /** turns 原始下标（含压缩分隔线位），跳转与高亮都用它对齐 */
  index: number
  num: number
  q: string
  preview: string
}

const entries = computed<OutlineEntry[]>(() => {
  const list: OutlineEntry[] = []
  let num = 0
  props.turns.forEach((turn, index) => {
    if (turn.kind === 'compact') return
    num += 1
    const q = clip(turn.q, Q_LIMIT)
    list.push({ index, num, q: q || t('pilot.outline.turn', { n: num }), preview: previewOf(turn) })
  })
  return list
})

const open = ref(false)
const root = ref<HTMLElement | null>(null)
const listEl = ref<HTMLElement | null>(null)

function revealActive() {
  void nextTick(() => {
    const list = listEl.value
    const row = list?.querySelector<HTMLElement>('[data-active="true"]')
    if (!list || !row) return
    // 只调面板自身 scrollTop——scrollIntoView 会连带滚页面，与跳转滚动打架
    const gap = 4
    const r = row.getBoundingClientRect()
    const l = list.getBoundingClientRect()
    if (r.top < l.top + gap) list.scrollTop -= l.top - r.top + gap
    else if (r.bottom > l.bottom - gap) list.scrollTop += r.bottom - l.bottom + gap
  })
}

function toggle() {
  open.value = !open.value
  emit('toggle', open.value)
  if (open.value) revealActive()
}

function onDocClick(e: MouseEvent) {
  if (open.value && root.value && !root.value.contains(e.target as Node)) toggle()
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))

watch(
  () => props.activeIndex,
  () => {
    if (open.value) revealActive()
  },
)
</script>

<template>
  <div ref="root" class="poutline">
    <button
      type="button"
      class="poutline__btn"
      :class="{ 'is-open': open }"
      :title="t('pilot.outline.title')"
      :aria-label="t('pilot.outline.title')"
      :aria-expanded="open"
      @click="toggle"
    >
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M4 6h16M4 12h10M4 18h7" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
      </svg>
    </button>
    <Transition name="hl-pop">
      <div v-if="open" ref="listEl" class="poutline__panel" :class="{ 'is-right': side === 'right' }">
        <div v-if="!entries.length" class="poutline__empty">{{ t('pilot.outline.empty') }}</div>
        <button
          v-for="it in entries"
          :key="it.index"
          type="button"
          class="poutline__row"
          :data-active="it.index === activeIndex ? 'true' : undefined"
          :aria-current="it.index === activeIndex ? 'true' : undefined"
          @click="emit('jump', it.index)"
        >
          <span class="poutline__num" aria-hidden="true">{{ it.num }}</span>
          <span class="poutline__main">
            <span class="poutline__q">{{ it.q }}</span>
            <span v-if="it.preview" class="poutline__preview">{{ it.preview }}</span>
          </span>
        </button>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.poutline {
  position: relative;
  flex: none;
}

.poutline__btn {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border: 1px dashed var(--border-soft);
  border-radius: 8px;
  color: var(--text-secondary);
  background: none;
  cursor: pointer;
}

.poutline__btn:hover,
.poutline__btn.is-open {
  border-color: var(--accent-a30);
  background: var(--accent-a10);
  color: var(--accent);
}

.poutline__btn svg {
  width: 14px;
  height: 14px;
}

.poutline__panel {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 6;
  width: 296px;
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

/* 会话列场景：面板向右弹（列贴着窗口左缘，向下弹会被视口裁剪） */
.poutline__panel.is-right {
  top: 0;
  right: auto;
  left: calc(100% + 6px);
  max-height: 420px;
}

.poutline__row {
  position: relative;
  display: flex;
  gap: 8px;
  align-items: baseline;
  width: 100%;
  padding: 6px 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
  text-align: left;
}

.poutline__row:hover,
.poutline__row[data-active='true'] {
  background: var(--accent-a10);
}

.poutline__row[data-active='true']::before {
  content: '';
  position: absolute;
  left: 0;
  top: 6px;
  bottom: 6px;
  width: 2px;
  border-radius: 1px;
  background: var(--accent);
}

.poutline__num {
  flex: none;
  min-width: 16px;
  font-size: 11px;
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.poutline__row[data-active='true'] .poutline__num {
  color: var(--accent);
}

.poutline__main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.poutline__q {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text-primary);
}

.poutline__preview {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  font-size: 11px;
  line-height: 1.45;
  color: var(--text-secondary);
}

.poutline__empty {
  padding: 12px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  text-align: center;
}
</style>
