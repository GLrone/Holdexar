<script setup lang="ts">
/**
 * 顶栏更新入口（常驻胶囊）：点击展开更新面板，面板内「检测更新」打开更新报告窗口。
 *
 * 胶囊三态着色：发现新版 / 待重启 = 成功绿，下载中 = 强调色 + 百分比，无事 = 中性。
 * 面板状态行按同一优先级取一句：下载中 > 待重启 > 发现新版 > 已是最新；
 * 启动后尚未检查过时只有标题与「检测更新」按钮。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import HlIcon from '@/components/ui/HlIcon.vue'
import HlSpinner from '@/components/ui/HlSpinner.vue'
import { useI18n } from '@/locales'
import { useUpdaterStore } from '@/stores/updater'

const updater = useUpdaterStore()
const { t } = useI18n()

const panelOpen = ref(false)
const rootRef = ref<HTMLElement | null>(null)

const version = computed(() =>
  (updater.info?.latest || updater.pendingTag || '').replace(/^v/, ''),
)

const percent = computed(() => Math.max(0, Math.min(100, updater.progress?.percent ?? 0)))

/** 面板状态行（tone 决定文字色）；无可陈述的状态时返回 null，面板只留标题与按钮 */
const statusLine = computed<{ text: string; tone: 'accent' | 'success' | 'muted' } | null>(() => {
  if (updater.downloading) {
    return { text: `${t('updateDialog.progressLabel')} ${percent.value}%`, tone: 'accent' }
  }
  if (updater.pendingTag) {
    return { text: t('updateDialog.headReady', { version: version.value }), tone: 'success' }
  }
  if (updater.hasUpdate) {
    return updater.info?.latest
      ? { text: t('updateDialog.headAvailable', { version: version.value }), tone: 'success' }
      : null
  }
  if (updater.checked && updater.info && updater.info.available === false) {
    const current = (updater.info.current || '').replace(/^v/, '')
    return current
      ? { text: t('updateDialog.upToDate', { version: current }), tone: 'muted' }
      : { text: t('updateDialog.headLatest'), tone: 'muted' }
  }
  return null
})

/** 有事可报 = 胶囊离开中性态（绿 / 强调色），无事时仍是可点入口 */
const noteworthy = computed(
  () => updater.hasUpdate || Boolean(updater.pendingTag) || updater.downloading,
)

function togglePanel() {
  panelOpen.value = !panelOpen.value
}

/** 面板「检测更新」：走全局弹窗唯一出口（openDialog 自带强制检查） */
function checkNow() {
  panelOpen.value = false
  void updater.openDialog()
}

function onDocClick(e: MouseEvent) {
  if (rootRef.value && !rootRef.value.contains(e.target as Node)) panelOpen.value = false
}

onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>

<template>
  <div ref="rootRef" class="upd-entry-host">
    <button
      type="button"
      class="upd-entry"
      :class="{ 'is-noteworthy': noteworthy, 'is-downloading': updater.downloading }"
      :title="t('updateDialog.entryTip')"
      :aria-expanded="panelOpen"
      @click="togglePanel"
    >
      <HlSpinner v-if="updater.downloading" class="upd-entry__spin" />
      <HlIcon v-else name="download" :size="13" />
      <span class="upd-entry__txt">
        {{ updater.downloading ? `${percent}%` : t('updateDialog.entry') }}
      </span>
    </button>

    <Transition name="upd-entry-pop">
      <div v-if="panelOpen" class="upd-entry-panel">
        <p class="upd-entry-panel__title">{{ t('updateDialog.title') }}</p>
        <p
          v-if="statusLine"
          class="upd-entry-panel__status"
          :class="`is-${statusLine.tone}`"
        >
          {{ statusLine.text }}
        </p>
        <button type="button" class="upd-entry-panel__check" @click="checkNow">
          {{ t('updateDialog.checkNow') }}
        </button>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.upd-entry-host {
  position: relative;
  display: inline-flex;
}

.upd-entry {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: var(--bg-card);
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
  cursor: pointer;
  transition:
    background calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout),
    border-color calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout),
    color calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout);
}

.upd-entry:hover,
.upd-entry.is-open {
  border-color: var(--border-strong);
  color: var(--text-primary);
}

.upd-entry.is-noteworthy {
  border-color: var(--success-a40);
  background: var(--success-a15);
  color: var(--success);
}

.upd-entry.is-noteworthy:hover {
  background: var(--success-a30);
  border-color: var(--success-a60);
}

/* 下载中改中性底：它此时表达的是「在跑」而不是「有好消息」 */
.upd-entry.is-downloading {
  background: var(--accent-a10);
  border-color: var(--accent-a40);
  color: var(--accent);
}

.upd-entry.is-downloading:hover {
  background: var(--accent-a20);
}

.upd-entry__spin {
  width: 12px;
  height: 12px;
  border-width: 2px;
}

.upd-entry__txt {
  font-variant-numeric: tabular-nums;
}

.upd-entry-panel {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 60;
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 15rem;
  max-width: calc(100vw - 2rem);
  padding: 12px;
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  text-align: left;
}

.upd-entry-panel__title {
  margin: 0;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-primary);
}

.upd-entry-panel__status {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
}

.upd-entry-panel__status.is-accent {
  color: var(--accent);
}

.upd-entry-panel__status.is-success {
  color: var(--success);
}

.upd-entry-panel__status.is-muted {
  color: var(--text-muted);
}

.upd-entry-panel__check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid var(--accent-a40);
  border-radius: 8px;
  background: var(--accent-a10);
  color: var(--accent);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition:
    background calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout),
    border-color calc(var(--duration-1) * var(--motion-scale)) var(--ease-inout);
}

.upd-entry-panel__check:hover {
  background: var(--accent-a20);
  border-color: var(--accent-a60);
}

/* 出入场对称：位移 + 透明度，时长走总闸（--motion-scale 归零即瞬时） */
.upd-entry-pop-enter-active,
.upd-entry-pop-leave-active {
  transition:
    opacity calc(var(--duration-2) * var(--motion-scale)) var(--ease-inout),
    transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-inout);
}

.upd-entry-pop-enter-from,
.upd-entry-pop-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
