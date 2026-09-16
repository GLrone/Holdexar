<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { systemApi, LOGS_STREAM_URL } from '@/api/client'
import { useI18n } from '@/locales'
import { HlButton, HlIcon, message } from '@/components/ui'

/**
 * 运行日志：进入页面先拉环形缓冲最近 300 行，再经 SSE 续播增量。
 * 跟随滚动 = 贴底时新行自动滚到底；用户上翻即暂停跟随，「回到底部」一键恢复。
 * 复制：正文直接拖选后 Ctrl+C（拖选期间新日志不抢视口）；按钮复制全部缓冲。
 */

const MAX_LINES = 1500 // 前端保留上限（超出丢最旧行，避免长驻页面内存增长）
const REPLAY = 300 // 进页回放行数

const { t } = useI18n()

const lines = ref<string[]>([])
const connected = ref(false)
const paused = ref(false)
const logBoxRef = ref<HTMLElement | null>(null)

let source: EventSource | null = null

async function scrollToBottom() {
  await nextTick()
  const el = logBoxRef.value
  if (el) el.scrollTop = el.scrollHeight
}

/** 用户是否正贴着底部（距底 24px 内视为贴底） */
function isAtBottom(): boolean {
  const el = logBoxRef.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 24
}

/** 日志框内是否存在拖选中的文本（有选区时视口归用户，新行不许抢滚动） */
function hasSelection(): boolean {
  const sel = window.getSelection()
  if (!sel || sel.isCollapsed) return false
  const box = logBoxRef.value
  return !!box && !!sel.anchorNode && box.contains(sel.anchorNode)
}

function push(line: string) {
  lines.value.push(line)
  if (lines.value.length > MAX_LINES) lines.value.splice(0, lines.value.length - MAX_LINES)
  if (!paused.value && !hasSelection()) scrollToBottom()
}

/** 滚动位置驱动跟随：离开底部即暂停，滚回底部自动恢复（scroll 在滚动生效后触发）*/
function onScroll() {
  paused.value = !isAtBottom()
}

/** 「回到底部并跟随」：清除拖选（选区不清会继续压住跟随）→ 滚到底并恢复跟随 */
function resumeFollow() {
  window.getSelection()?.removeAllRanges()
  paused.value = false
  scrollToBottom()
}

/* ── 复制（正文拖选 + Ctrl+C / 按钮复制全部缓冲）── */

const copiedAll = ref(false)

/** 写剪贴板：clipboard API 优先，旧 WebView2 无权限时回落 execCommand */
async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* 权限拒绝/无焦点 → 走回落 */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch {
    return false
  }
}

async function copyAll() {
  if (!lines.value.length) return
  const ok = await writeClipboard(lines.value.join('\n'))
  if (!ok) {
    message.error(t('logs.copy.failedHint'))
    return
  }
  copiedAll.value = true
  window.setTimeout(() => (copiedAll.value = false), 1500)
}

async function load() {
  try {
    const res = await systemApi.logs(REPLAY)
    lines.value = res.lines
    scrollToBottom()
  } catch {
    /* 回放失败不阻塞：SSE 增量照常 */
  }
}

function startStream() {
  source = new EventSource(LOGS_STREAM_URL)
  source.onopen = () => {
    connected.value = true
  }
  source.onmessage = (e) => {
    // 后端整行 JSON 编码（含换行的 traceback 也不会破坏 SSE 帧）
    try {
      push(JSON.parse(e.data) as string)
    } catch {
      push(e.data)
    }
  }
  source.onerror = () => {
    connected.value = false // EventSource 自动重连；重连成功 onopen 再次置位
  }
}

onMounted(() => {
  load()
  startStream()
})

onBeforeUnmount(() => {
  source?.close()
  source = null
})

/* ── 行结构着色：`时间 [级别] logger: 消息` ── */

function levelOf(line: string): 'error' | 'warning' | 'info' {
  if (line.includes('[ERROR]') || line.includes('[CRITICAL]')) return 'error'
  if (line.includes('[WARNING]')) return 'warning'
  return 'info'
}

const levelCount = computed(() => {
  let error = 0
  let warning = 0
  for (const line of lines.value) {
    const lv = levelOf(line)
    if (lv === 'error') error += 1
    else if (lv === 'warning') warning += 1
  }
  return { error, warning }
})
</script>

<template>
  <section class="logs-page">
    <div class="logs-toolbar">
      <div class="logs-toolbar__state">
        <span class="logs-dot" :class="{ on: connected }" />
        <span>{{ t(connected ? 'logs.stream.connected' : 'logs.stream.connecting') }}</span>
        <span class="logs-toolbar__sep">·</span>
        <span>{{ t('logs.count.lines', { n: lines.length }) }}</span>
        <template v-if="levelCount.error > 0 || levelCount.warning > 0">
          <span class="logs-toolbar__sep">·</span>
          <span class="logs-cnt logs-cnt--error">{{
            t('logs.count.errors', { n: levelCount.error })
          }}</span>
          <span class="logs-cnt logs-cnt--warning">{{
            t('logs.count.warnings', { n: levelCount.warning })
          }}</span>
        </template>
      </div>

      <div class="logs-toolbar__actions">
        <HlButton size="sm" :disabled="!paused" @click="resumeFollow">
          <HlIcon name="refresh" />{{ t(paused ? 'logs.action.resume' : 'logs.action.following') }}
        </HlButton>
        <HlButton size="sm" :disabled="!lines.length" @click="copyAll">
          <HlIcon name="copy" />{{
            copiedAll ? t('common.copied') : t('logs.action.copyAll', { n: lines.length })
          }}
        </HlButton>
        <HlButton size="sm" @click="lines = []">
          <HlIcon name="delete" />{{ t('logs.action.clear') }}
        </HlButton>
      </div>
    </div>

    <div ref="logBoxRef" class="logs-box" @scroll.passive="onScroll">
      <div v-if="lines.length === 0" class="logs-empty">
        {{ t('logs.empty') }}
      </div>
      <div v-for="(line, i) in lines" :key="i" class="logs-line" :class="`is-${levelOf(line)}`">{{
        line
      }}</div>
    </div>
  </section>
</template>

<style scoped>
.logs-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  /* 视口高 - 顶栏 56 - view-container 上下留白约 32 + 工具行自身高度 */
  height: calc(100vh - 56px - 32px);
  padding: 0 4px;
}

.logs-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  flex-shrink: 0;
}

.logs-toolbar__state {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-muted);
}

.logs-toolbar__sep {
  color: var(--text-faint);
}

.logs-cnt--error {
  color: var(--danger);
}

.logs-cnt--warning {
  color: var(--warning);
}

.logs-toolbar__actions {
  display: flex;
  gap: 8px;
}

.logs-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-faint);
  flex-shrink: 0;
}

.logs-dot.on {
  background: var(--success);
  box-shadow: 0 0 6px var(--success-a40);
}

.logs-box {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  background: var(--surface-inset);
  border: 1px solid var(--line-1);
  border-radius: var(--radius);
  padding: 12px 14px;
  font-family: ui-monospace, SFMono-Regular, Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.65;
}

.logs-line {
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-muted);
  /* 拖选复制：文本选择交还系统原生行为（选中后 Ctrl+C），拖选期间新日志
     不抢滚动（见 push），选完一段不会被滚走 */
  cursor: text;
  user-select: text;
}

.logs-line.is-warning {
  color: var(--warning);
}

.logs-line.is-error {
  color: var(--danger);
}

.logs-empty {
  color: var(--text-faint);
  font-size: 13px;
  padding: 24px 0;
  text-align: center;
}
</style>
