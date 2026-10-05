<script setup lang="ts">
/**
 * 思考链折叠行（对齐 ZCode 对话面板模式的自研实现）。
 * 三态：流式展开 =「正在思考 · 秒表计时 + 吸底跟随」（默认展开，用户可手收为
 * 「正在思考 · 最新一行滚动摘要」扫光态）；完成 =「思考 · 用时 N 秒」。
 * 初始开合由 initialOpen 定（历史过程链内传 false），用户手动收合过则不再被自动展开；
 * active 转 false（该阶段已让位给后续阶段）时自动收起，保证同时只展开一个思考块。
 * 展开体纯文本 pre-wrap——长思考逐帧重跑 markdown 解析代价过高，刻意不渲染。
 * 时长优先取外部服务端值（durSec），缺省回退内部秒表/不显示。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { useI18n } from '@/locales'

const props = withDefaults(
  defineProps<{
    text: string
    live?: boolean
    /** 完成态时长（秒，服务端工时）；缺省回退内部秒表，均无则不显示时长 */
    durSec?: number | null
    /** 初始开合（历史轮过程链内默认收起，链头一行已是摘要）；默认展开 */
    initialOpen?: boolean
    /** 首个 delta 前的等待话术（live 且无正文时作为行标签扫光显示） */
    waitingText?: string
    /** 是否为当前阶段（false = 已让位给后续阶段，自动收起；用户手动开过则不介入） */
    active?: boolean
  }>(),
  { live: false, durSec: null, initialOpen: true, active: true },
)

const { t } = useI18n()

const open = ref(props.initialOpen)
const userTouched = ref(false)
const liveSec = ref(0)
let ticker: ReturnType<typeof setInterval> | null = null

function stopTicker() {
  if (ticker) {
    clearInterval(ticker)
    ticker = null
  }
}

function toggle() {
  userTouched.value = true
  open.value = !open.value
  if (open.value && props.live && !ticker) {
    const start = Date.now() - liveSec.value * 1000
    ticker = setInterval(() => {
      liveSec.value = Math.floor((Date.now() - start) / 1000)
    }, 1000)
  }
  if (!open.value) stopTicker()
}

watch(
  () => props.live,
  (live) => {
    // 流式→完成边界：只停秒表，不收起——思考对用户保持可见（用户手收过则保持收起）
    if (!live) stopTicker()
  },
)
onBeforeUnmount(stopTicker)

watch(
  () => props.active,
  (active) => {
    // 阶段让位：不再是当前阶段即自动收起，避免多个思考块同时展开
    if (!active && !userTouched.value) open.value = false
  },
)

/* 流式摘要 = 思考文本最后一个非空行，视口始终钉在最右（旧字左移新字滚入）。
   从尾部逐行回退取行，不整串 split——长思考下每帧切分整串是主线程负担。 */
const summary = computed(() => {
  const text = props.text || ''
  let end = text.length
  for (let i = 0; i < 32 && end > 0; i++) {
    const cut = text.lastIndexOf('\n', end - 1)
    const line = text.slice(cut + 1, end).trim()
    if (line) return line
    if (cut < 0) break
    end = cut
  }
  return ''
})

const summaryBox = ref<HTMLDivElement | null>(null)
// post：回调排在 DOM 补丁之后，此时才量得到新行宽
watch(
  summary,
  () => {
    const el = summaryBox.value
    if (el) el.scrollLeft = el.scrollWidth
  },
  { flush: 'post' },
)

/* 展开体吸底跟随：用户滚离底部 4px 以上即暂停，滚回底部自动恢复 */
const bodyBox = ref<HTMLDivElement | null>(null)
const follow = ref(true)

function onBodyScroll() {
  const el = bodyBox.value
  if (!el) return
  follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 4
}

function pinBottom() {
  if (!follow.value) return
  const el = bodyBox.value
  if (el) el.scrollTop = el.scrollHeight
}

/* 吸底必须与渲染同帧：post 回调在 DOM 补丁之后、本帧绘制之前，读一次布局即钉到底。
   若延后到定时器节流，正文每长出一行就滞后一行高再被拽回，表现为整块上下跳动。 */
watch(
  () => props.text,
  () => {
    if (props.live && open.value) pinBottom()
  },
  { flush: 'post' },
)
watch(
  open,
  (o) => {
    if (o && props.live) pinBottom()
  },
  { flush: 'post' },
)

const durLabel = computed(() => {
  if (props.live && !props.text && props.waitingText) return props.waitingText
  if (props.live) {
    return open.value && liveSec.value
      ? t('pilot.think.liveElapsed', { sec: liveSec.value })
      : t('pilot.think.live')
  }
  const sec = props.durSec ?? liveSec.value
  return sec ? t('pilot.think.done', { sec }) : t('pilot.think.plain')
})
</script>

<template>
  <div class="pthink" :class="{ 'is-open': open, 'is-live': live }">
    <button type="button" class="pthink__row" @click="toggle">
      <span class="pthink__icon" aria-hidden="true"></span>
      <span class="pthink__label" :class="{ 'is-shimmer': live && (!open || !text) }">{{ durLabel }}</span>
      <span v-if="live && !open && summary" class="pthink__summary-box">
        <span ref="summaryBox" class="pthink__summary">{{ summary }}</span>
      </span>
      <span class="pthink__chevron" aria-hidden="true"></span>
    </button>
    <div v-if="open && text" ref="bodyBox" class="pthink__body-box" @scroll="onBodyScroll">
      <div class="pthink__body">{{ text }}</div>
    </div>
  </div>
</template>

<style scoped>
.pthink {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  /* 占满所在阶段宽度：不写则按内容收缩，长思考正文右侧留白 */
  align-self: stretch;
  max-width: 100%;
}

.pthink__row {
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

.pthink__icon {
  flex-shrink: 0;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  border: 1.5px solid var(--text-faint);
}

.pthink__label {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-muted);
}

/* 流式扫光：渐变背景裁切文字，替代一切旋转 loader */
.pthink__label.is-shimmer {
  background: linear-gradient(90deg, var(--text-faint) 34%, var(--text-secondary) 50%, var(--text-faint) 66%);
  background-size: 300% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: pthink-sweep 4s linear infinite;
}

@keyframes pthink-sweep {
  to {
    background-position: -300% 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .pthink__label.is-shimmer {
    animation: none;
    background: none;
    color: var(--text-secondary);
  }
}

/* 单行滚动摘要：两端渐隐 mask，内容增长视口钉最右 */
.pthink__summary-box {
  min-width: 0;
  overflow: hidden;
  max-width: 220px;
  mask-image: linear-gradient(90deg, transparent, #000 16px, #000 calc(100% - 16px), transparent);
  -webkit-mask-image: linear-gradient(90deg, transparent, #000 16px, #000 calc(100% - 16px), transparent);
}

.pthink__summary {
  display: inline-block;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text-secondary);
}

.pthink__chevron {
  flex-shrink: 0;
  width: 7px;
  height: 7px;
  border-right: 1.5px solid var(--text-faint);
  border-bottom: 1.5px solid var(--text-faint);
  transform: rotate(-45deg);
  opacity: 0;
  transition: transform 0.2s, opacity 0.2s;
}

.pthink__row:hover .pthink__chevron,
.pthink.is-open .pthink__chevron {
  opacity: 1;
}

.pthink.is-open .pthink__chevron {
  transform: rotate(45deg) translate(-2px, -2px);
}

/* 展开体：纯文本 pre-wrap + 左导线 + 限高滚动 + 吸底跟随；
   撑满思考块宽度，滚动条贴阶段右缘而不是贴正文右缘 */
.pthink__body-box {
  align-self: stretch;
  margin-top: 6px;
  margin-left: 2px;
  max-height: 240px;
  overflow-y: auto;
  padding-left: 12px;
  border-left: 1px solid var(--border-soft);
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-muted);
}

.pthink__body {
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
