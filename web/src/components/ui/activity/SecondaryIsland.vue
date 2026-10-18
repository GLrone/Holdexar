<script setup lang="ts">
/**
 * SecondaryIsland — 副岛紧凑胶囊组件脚手架
 * 多任务时在主岛旁侧呈现次要活动的精简图标与计数，点击可交换为主岛。
 */
import { computed } from 'vue'
import { useI18n } from '@/locales'
import HlIcon from '@/components/ui/HlIcon.vue'
import HlSpinner from '@/components/ui/HlSpinner.vue'
import type { Activity } from './types'

const props = defineProps<{
  activity: Activity | null
  extraCount?: number
}>()

const emit = defineEmits<{
  (e: 'swap'): void
  (e: 'openOverview'): void
}>()

const { t } = useI18n()

const iconName = computed(() => {
  if (!props.activity) return 'info'
  if (props.activity.icon) return props.activity.icon
  if (props.activity.status === 'failed') return 'x-circle'
  if (props.activity.status === 'success') return 'check-circle'
  if (props.activity.status === 'running') return 'refresh'
  return 'info'
})

const isSpinning = computed(() => props.activity?.status === 'running')

const toneClass = computed(() => {
  if (!props.activity) return ''
  if (props.activity.status === 'failed') return 'is-error'
  if (props.activity.status === 'success') return 'is-success'
  if (props.activity.status === 'running') return 'is-progress'
  if (props.activity.tone) return `is-${props.activity.tone}`
  return ''
})

const tooltipText = computed(() => {
  if (!props.activity) return ''
  const swapLabel = t('island.swap')
  return `${props.activity.title} · ${swapLabel}`
})

function onSecondaryClick(e: MouseEvent) {
  e.stopPropagation()
  emit('swap')
}

function onSecondaryKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    e.stopPropagation()
    emit('swap')
  }
}
</script>

<template>
  <div
    v-if="activity"
    class="hl-secondary-island"
    role="button"
    tabindex="0"
    :title="tooltipText"
    :aria-label="tooltipText"
    @click="onSecondaryClick"
    @keydown="onSecondaryKeydown"
  >
    <div class="hl-secondary-island__lead">
      <HlSpinner v-if="isSpinning" class="hl-secondary-island__spin" />
      <HlIcon v-else :name="iconName" :size="12" class="hl-secondary-island__icon" :class="toneClass" />
    </div>
    <span v-if="extraCount && extraCount > 0" class="hl-secondary-island__count">
      +{{ extraCount }}
    </span>
  </div>
</template>

<style scoped>
.hl-secondary-island {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 8px;
  border-radius: 15px;
  background: var(--island-bg);
  box-shadow: var(--shadow-island);
  color: var(--text-primary);
  cursor: pointer;
  user-select: none;
  transition:
    transform calc(var(--duration-3) * var(--motion-scale)) var(--ease-island),
    box-shadow calc(var(--duration-3) * var(--motion-scale)) var(--ease-out),
    opacity calc(var(--duration-3) * var(--motion-scale)) var(--ease-out);
}

.hl-secondary-island:hover {
  transform: scale(1.05);
}

.hl-secondary-island:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.hl-secondary-island__lead {
  display: flex;
  align-items: center;
  justify-content: center;
}

.hl-secondary-island__spin {
  width: 12px;
  height: 12px;
}

.hl-secondary-island__icon.is-success {
  color: var(--success);
}

.hl-secondary-island__icon.is-error {
  color: var(--danger);
}

.hl-secondary-island__icon.is-progress {
  color: var(--accent);
}

.hl-secondary-island__count {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-muted);
}
</style>
