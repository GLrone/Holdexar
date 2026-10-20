<script setup lang="ts">
/**
 * ActivityPanel — 活动总览列表面板组件脚手架
 * 展开呈现全部在跑与异常活动，允许用户直接点选主岛或导航业务页面。
 */
import { useI18n } from '@/locales'
import ActivityItem from './ActivityItem.vue'
import type { Activity } from './types'

defineProps<{
  activities: readonly Activity[]
  primaryId: string | null
  pinnedId?: string | null
  open: boolean
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'pin', id: string): void
  (e: 'action', activity: Activity): void
  (e: 'close'): void
}>()

const { t } = useI18n()
</script>

<template>
  <Transition name="activity-panel">
    <div
      v-if="open"
      class="hl-activity-panel"
      role="dialog"
      :aria-label="t('island.hub')"
      tabindex="-1"
      @keydown.esc.stop="emit('close')"
    >
      <div class="hl-activity-panel__header">
        <span class="hl-activity-panel__title">{{ t('island.hub') }}</span>
        <button
          type="button"
          class="hl-activity-panel__close"
          :title="t('island.dismiss')"
          :aria-label="t('island.dismiss')"
          @click="emit('close')"
        >
          ✕
        </button>
      </div>

      <div class="hl-activity-panel__list">
        <ActivityItem
          v-for="act in activities"
          :key="act.id"
          :activity="act"
          :is-primary="act.id === primaryId"
          :is-pinned="act.id === pinnedId"
          @select="emit('select', act.id)"
          @pin="emit('pin', act.id)"
          @action="emit('action', act)"
        />
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.hl-activity-panel {
  --text-primary: var(--island-panel-fg);
  --text-secondary: var(--island-panel-fg);
  --text-muted: var(--island-panel-muted);
  --island-btn: var(--island-panel-surface);
  --island-bg: var(--island-panel-bg);
  --surface-track: var(--island-shell-track);
  --surface-hover: var(--island-panel-hover);
  --border-soft: var(--island-panel-divider);
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  right: auto;
  transform: translateX(-50%);
  transform-origin: top center;
  width: min(320px, calc(100vw - 24px));
  max-height: 380px;
  border-radius: 16px;
  background: var(--island-bg);
  box-shadow: var(--shadow-island);
  border: none;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  z-index: 10003;
}

.hl-activity-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-soft);
}

.hl-activity-panel__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.hl-activity-panel__close {
  border: none;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 12px;
}

.hl-activity-panel__list {
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow-y: auto;
  max-height: 320px;
}

.activity-panel-enter-active,
.activity-panel-leave-active {
  transition: opacity calc(var(--duration-1) * var(--motion-scale)) var(--ease-out),
              transform calc(var(--duration-1) * var(--motion-scale)) var(--ease-island);
}

.activity-panel-enter-from,
.activity-panel-leave-to {
  opacity: 0;
  transform: translate(-50%, -8px);
}
</style>
