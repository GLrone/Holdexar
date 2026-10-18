<script setup lang="ts">
/**
 * ActivityItem — 活动总览列表项组件脚手架
 * 呈现单个活动的图标、标题、进度或异常状态，并支持设为主岛或跳转。
 */
import { computed } from 'vue'
import HlIcon from '@/components/ui/HlIcon.vue'
import HlSpinner from '@/components/ui/HlSpinner.vue'
import { useI18n } from '@/locales'
import type { Activity } from './types'

const props = defineProps<{
  activity: Activity
  isPrimary?: boolean
  isPinned?: boolean
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'pin', id: string): void
  (e: 'action', activity: Activity): void
}>()

const { t } = useI18n()

const iconName = computed(() => {
  if (props.activity.icon) return props.activity.icon
  if (props.activity.status === 'failed') return 'x-circle'
  if (props.activity.status === 'success') return 'check-circle'
  if (props.activity.status === 'running') return 'refresh'
  return 'info'
})

const isSpinning = computed(() => props.activity.status === 'running')

const toneClass = computed(() => {
  if (props.activity.status === 'failed') return 'is-error'
  if (props.activity.status === 'success') return 'is-success'
  if (props.activity.status === 'running') return 'is-progress'
  if (props.activity.tone) return `is-${props.activity.tone}`
  return ''
})

function onRowClick() {
  emit('select', props.activity.id)
}

function onActionClick(e: MouseEvent) {
  e.stopPropagation()
  emit('action', props.activity)
}
</script>

<template>
  <div
    class="hl-activity-item"
    :class="{ 'is-primary': isPrimary, 'is-attention': activity.needsAttention }"
    role="button"
    tabindex="0"
    @click="onRowClick"
  >
    <div class="hl-activity-item__lead">
      <HlSpinner v-if="isSpinning" class="hl-activity-item__spin" />
      <HlIcon v-else :name="iconName" :size="14" class="hl-activity-item__icon" :class="toneClass" />
    </div>

    <div class="hl-activity-item__content">
      <div class="hl-activity-item__header">
        <span class="hl-activity-item__title">{{ activity.title }}</span>
        <span v-if="isPrimary" class="hl-activity-item__badge">{{ t('island.view') }}</span>
      </div>
      <p v-if="activity.detail" class="hl-activity-item__detail">{{ activity.detail }}</p>

      <div v-if="activity.progress !== undefined" class="hl-activity-item__bar">
        <div
          class="hl-activity-item__fill"
          :style="activity.progress !== null ? { width: `${activity.progress}%` } : undefined"
          :class="{ 'is-slide': activity.progress === null }"
        />
      </div>
    </div>

    <div class="hl-activity-item__actions">
      <button
        v-if="activity.to"
        type="button"
        class="hl-activity-item__btn"
        @click="onActionClick"
      >
        {{ activity.toLabel || t('island.view') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.hl-activity-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--island-btn);
  cursor: pointer;
  transition: background var(--transition);
}

.hl-activity-item:hover {
  background: var(--surface-hover);
}

.hl-activity-item.is-primary {
  border: 1px solid var(--accent);
}

.hl-activity-item.is-attention {
  border-left: 3px solid var(--danger);
}

.hl-activity-item__lead {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
}

.hl-activity-item__icon.is-success {
  color: var(--success);
}

.hl-activity-item__icon.is-error {
  color: var(--danger);
}

.hl-activity-item__icon.is-progress {
  color: var(--accent);
}

.hl-activity-item__content {
  flex: 1 1 auto;
  min-width: 0;
}

.hl-activity-item__header {
  display: flex;
  align-items: center;
  gap: 6px;
}

.hl-activity-item__title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hl-activity-item__badge {
  font-size: 10px;
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--accent-fill);
  color: var(--on-accent-fill);
}

.hl-activity-item__detail {
  margin: 2px 0 0;
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hl-activity-item__bar {
  position: relative;
  height: 2px;
  background: var(--surface-track);
  border-radius: 1px;
  overflow: hidden;
  margin-top: 4px;
}

.hl-activity-item__fill {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  background: var(--accent);
}

.hl-activity-item__fill.is-slide {
  width: 40%;
  animation: hlIslandBar 1.4s var(--ease-inout) infinite;
}

.hl-activity-item__btn {
  border: none;
  background: transparent;
  color: var(--accent);
  font-size: 11px;
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
}

.hl-activity-item__btn:hover {
  text-decoration: underline;
}
</style>
