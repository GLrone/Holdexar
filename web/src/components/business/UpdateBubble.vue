<script setup lang="ts">
/**
 * 左上角更新气泡：有新版/下载中/待重启/上次失败时悬浮出现，其余时刻不存在。
 *
 * 状态单一来源 = updater store（本组件零私有判据，只做投影与点击转发）：
 *   待重启 > 下载中 > 换装失败 > 发现新版，同一时刻只呈现最高优先级的一态。
 * 悬停浮层给更新内容（下载中给已下载/总大小），点击打开更新报告窗口。
 * 常驻更新面板与顶栏胶囊已由本气泡替代。
 */
import { computed, ref } from 'vue'

import HlIcon from '@/components/ui/HlIcon.vue'
import HlSpinner from '@/components/ui/HlSpinner.vue'
import { useI18n } from '@/locales'
import { useSettingsStore } from '@/stores/settings'
import { useUpdaterStore } from '@/stores/updater'

import UpdateNotes from './UpdateNotes.vue'

const updater = useUpdaterStore()
const settings = useSettingsStore()
const { t } = useI18n()

const hovered = ref(false)

/** 展示版本：待重启以暂存 tag 为准（检查结果会被后台检查覆盖成旧 latest），
 *  其余取清单最新版 */
const version = computed(() =>
  (updater.pendingTag || updater.info?.latest || '').replace(/^v/, ''),
)

/** 跳过判定（严格等值）：跳过 0.4.0 只拦 0.4.0，拦不住之后的 0.5.0 */
const skippedNow = computed(() => {
  const s = updater.skipped
  return Boolean(s) && s === version.value
})

type BubbleState = 'ready' | 'downloading' | 'swapFailed' | 'available'

/** 可见性：进行中状态（下载/待重启/失败）无视提示开关与跳过，强制可见 */
const state = computed<BubbleState | null>(() => {
  if (updater.pendingTag) return 'ready'
  if (updater.downloading) return 'downloading'
  if (updater.swapFailed) return 'swapFailed'
  if (updater.hasUpdate && !skippedNow.value && settings.updateNotify !== false) {
    return 'available'
  }
  return null
})

const text = computed(() => {
  switch (state.value) {
    case 'ready':
      return t('update.bubble.ready', { version: version.value })
    case 'downloading':
      return t('update.bubble.downloading', {
        percent: Math.max(0, Math.min(100, updater.progress?.percent ?? 0)),
      })
    case 'swapFailed':
      return t('update.bubble.swapFailed')
    default:
      return t('update.bubble.available', { version: version.value })
  }
})

function fmtSize(bytes: number | null | undefined): string {
  const v = bytes || 0
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(0)} KB`
  if (v < 1024 * 1024 * 1024) return `${(v / 1024 / 1024).toFixed(1)} MB`
  return `${(v / 1024 / 1024 / 1024).toFixed(2)} GB`
}

/** 悬停浮层的下载进度行（下载中才有）：已下载 / 总大小 */
const sizeLine = computed(() => {
  const p = updater.progress
  if (!p || !p.total) return ''
  return `${fmtSize(p.received)} / ${fmtSize(p.total)}`
})

const releaseDate = computed(() => (updater.info?.publishedAt || '').slice(0, 10))

function open(): void {
  hovered.value = false
  void updater.openDialog()
}
</script>

<template>
  <Teleport to="body">
    <Transition name="upd-bubble-pop">
      <div v-if="state" class="upd-bubble" :class="`is-${state}`">
        <button
          type="button"
          class="upd-bubble__pill"
          :aria-label="text"
          @click="open"
          @mouseenter="hovered = true"
          @mouseleave="hovered = false"
        >
          <HlSpinner v-if="state === 'downloading'" class="upd-bubble__spin" />
          <HlIcon v-else :name="state === 'ready' ? 'check-circle' : 'download'" :size="13" />
          <span class="upd-bubble__txt">{{ text }}</span>
        </button>

        <!-- 悬停浮层：更新内容（下载中换成已下载/总大小），点击进完整窗口 -->
        <Transition name="upd-bubble-pop">
          <div
            v-if="hovered"
            class="upd-bubble__pop"
            @mouseenter="hovered = true"
            @mouseleave="hovered = false"
          >
            <template v-if="state === 'downloading'">
              <p class="upd-bubble__pop-title">{{ t('updateDialog.headDownloading', { version: version || '' }) }}</p>
              <p class="upd-bubble__pop-line hl-num">
                {{ sizeLine || t('update.bubble.hoverPreparing') }}
              </p>
            </template>
            <template v-else>
              <p class="upd-bubble__pop-title">
                {{ t('updateDialog.notesTitle', { version: version || '' }) }}
              </p>
              <p v-if="releaseDate" class="upd-bubble__pop-date">{{ t('updateDialog.releaseDate', { date: releaseDate }) }}</p>
              <div class="upd-bubble__pop-notes">
                <UpdateNotes :notes="updater.info?.notes || ''" :version="version" />
                <p v-if="!updater.info?.notes" class="upd-bubble__pop-line">
                  {{ t('updateDialog.notesEmpty') }}
                </p>
              </div>
              <p class="upd-bubble__pop-hint">{{ t('update.bubble.clickHint') }}</p>
            </template>
          </div>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 悬浮在窗口左上角（侧边栏品牌区下方）：z 走 teleport 弹层档位之上、
   菜单幕布之下（阶梯表见 learnings/frontend.md） */
.upd-bubble {
  position: fixed;
  top: 64px;
  left: 12px;
  z-index: 1200;
  display: flex;
}

.upd-bubble__pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 220px;
  padding: 5px 12px;
  border: 1px solid var(--success-a40);
  border-radius: 999px;
  background: var(--surface-pop, var(--bg-card));
  box-shadow: var(--shadow-md);
  color: var(--success);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
  cursor: pointer;
  transition:
    transform calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout),
    box-shadow calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout),
    border-color calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout),
    color calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout);
}

.upd-bubble__pill:hover {
  transform: translateY(-1px);
  box-shadow: var(--shadow-lg);
}

.upd-bubble.is-downloading .upd-bubble__pill {
  border-color: var(--accent-a40);
  color: var(--accent);
}

.upd-bubble.is-swapFailed .upd-bubble__pill {
  border-color: var(--danger-a40);
  color: var(--danger);
}

.upd-bubble__spin {
  width: 12px;
  height: 12px;
  border-width: 2px;
}

.upd-bubble__txt {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

/* 悬停浮层：气泡右侧展开，样式与更新窗口同一语汇（卡面 + 软边框） */
.upd-bubble__pop {
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  width: 340px;
  max-width: calc(100vw - 24px);
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 13px 14px;
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  background: var(--surface-pop-deep, var(--bg-card));
  box-shadow: var(--shadow-lg);
  cursor: default;
}

.upd-bubble__pop-title {
  margin: 0;
  font-size: 13px;
  font-weight: 700;
  color: var(--text-primary);
}

.upd-bubble__pop-date {
  margin: 0;
  font-size: 11px;
  color: var(--text-muted);
}

.upd-bubble__pop-notes {
  max-height: 220px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.65;
  color: var(--text-secondary);
}

.upd-bubble__pop-line {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
}

.upd-bubble__pop-hint {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--text-muted);
}

/* 出入场：位移 + 透明度（不用 scale，挂 --motion-scale 总闸） */
.upd-bubble-pop-enter-active,
.upd-bubble-pop-leave-active {
  transition:
    opacity calc(var(--duration-2) * var(--motion-scale)) var(--ease-inout),
    transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-inout);
}

.upd-bubble-pop-enter-from,
.upd-bubble-pop-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
</style>
