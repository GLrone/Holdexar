<script setup lang="ts">
/**
 * SecondaryIsland — 副岛紧凑胶囊组件
 * 多任务时在主岛旁侧呈现次要活动的精简图标与计数，点击可交换为主岛。
 * 支持悬停预览详情、点击余量进入总览面板与键盘无障碍访问。
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import { useI18n } from '@/locales'
import HlIcon from '@/components/ui/HlIcon.vue'
import HlSpinner from '@/components/ui/HlSpinner.vue'
import type { Activity } from './types'

const props = defineProps<{
  activity: Activity | null
  extraCount?: number
  isSwapping?: boolean
}>()

const emit = defineEmits<{
  (e: 'swap'): void
  (e: 'openOverview'): void
}>()

const { t } = useI18n()

const showPreview = ref(false)
let previewTimer: ReturnType<typeof setTimeout> | null = null

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
  showPreview.value = false
  emit('swap')
}

function onSecondaryKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    e.stopPropagation()
    showPreview.value = false
    emit('swap')
  } else if (e.key === 'o' || e.key === 'O') {
    e.preventDefault()
    e.stopPropagation()
    showPreview.value = false
    emit('openOverview')
  } else if (e.key === 'Escape') {
    showPreview.value = false
  }
}

function onMouseEnter() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(() => {
    showPreview.value = true
  }, 180)
}

function onMouseLeave() {
  if (previewTimer) clearTimeout(previewTimer)
  showPreview.value = false
}

onBeforeUnmount(() => {
  if (previewTimer) clearTimeout(previewTimer)
})
</script>

<template>
  <div
    v-if="activity"
    class="hl-secondary-island"
    :class="{ 'is-swapping-secondary': isSwapping }"
    role="button"
    tabindex="0"
    :title="tooltipText"
    :aria-label="tooltipText"
    @click="onSecondaryClick"
    @keydown="onSecondaryKeydown"
    @mouseenter="onMouseEnter"
    @mouseleave="onMouseLeave"
    @contextmenu.prevent="emit('openOverview')"
  >
    <div class="hl-secondary-island__lead">
      <HlSpinner v-if="isSpinning" class="hl-secondary-island__spin" />
      <HlIcon v-else :name="iconName" :size="12" class="hl-secondary-island__icon" :class="toneClass" />
    </div>

    <!-- 余量计数徽章：点击可直接进入活动总览面板 -->
    <span
      v-if="extraCount && extraCount > 0"
      class="hl-secondary-island__count"
      :title="t('island.hub')"
      :aria-label="t('island.hub')"
      @click.stop="emit('openOverview')"
    >
      +{{ extraCount }}
    </span>

    <!-- 悬停即时预览浮层 -->
    <Transition name="secondary-preview-fade">
      <div
        v-if="showPreview && activity"
        class="hl-secondary-preview"
        role="tooltip"
        @click.stop
      >
        <div class="hl-secondary-preview__header">
          <div class="hl-secondary-preview__title-wrap">
            <span class="hl-secondary-preview__title">{{ activity.title }}</span>
            <span class="hl-secondary-preview__source">{{ activity.source }}</span>
          </div>
          <span v-if="activity.progress !== undefined && activity.progress !== null" class="hl-secondary-preview__pct">
            {{ activity.progress }}%
          </span>
        </div>
        <p v-if="activity.detail" class="hl-secondary-preview__detail">
          {{ activity.detail }}
        </p>
        <div v-if="activity.progress !== undefined" class="hl-secondary-preview__bar">
          <i
            :style="activity.progress !== null ? { width: `${activity.progress}%` } : undefined"
            :class="{ 'is-slide': activity.progress === null }"
          />
        </div>
        <div class="hl-secondary-preview__tip">
          {{ t('island.previewHint') }}
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.hl-secondary-island {
  position: relative;
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

.hl-secondary-island.is-swapping-secondary {
  transform: translateX(-12px) scale(1.08);
  opacity: 0.82;
  transition: transform 140ms var(--ease-island), opacity 140ms var(--ease-out);
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
  padding: 1px 4px;
  border-radius: 8px;
  transition: background var(--transition), color var(--transition);
}

.hl-secondary-island__count:hover {
  background: var(--island-btn);
  color: var(--accent);
}

.hl-secondary-preview {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 220px;
  padding: 10px 12px;
  border-radius: 10px;
  background: var(--island-bg);
  box-shadow: var(--shadow-island);
  border: 1px solid var(--border-soft);
  color: var(--text-primary);
  display: flex;
  flex-direction: column;
  gap: 6px;
  z-index: 10001;
  pointer-events: auto;
  text-align: left;
}

.hl-secondary-preview__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 6px;
}

.hl-secondary-preview__title-wrap {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.hl-secondary-preview__title {
  font-size: 12px;
  font-weight: 600;
  line-height: 1.3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hl-secondary-preview__source {
  font-size: 10px;
  color: var(--text-muted);
  text-transform: uppercase;
}

.hl-secondary-preview__pct {
  font-size: 11px;
  font-weight: 600;
  font-family: var(--font-mono);
  color: var(--accent);
}

.hl-secondary-preview__detail {
  margin: 0;
  font-size: 11px;
  line-height: 1.4;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.hl-secondary-preview__bar {
  position: relative;
  height: 2px;
  border-radius: 1px;
  background: var(--surface-track);
  overflow: hidden;
}

.hl-secondary-preview__bar > i {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  border-radius: 1px;
  background: var(--accent);
}

.hl-secondary-preview__bar > i.is-slide {
  width: 40%;
  animation: hlIslandBar 1.4s var(--ease-inout) infinite;
}

.hl-secondary-preview__tip {
  font-size: 10px;
  color: var(--text-muted);
  border-top: 1px solid var(--border-soft);
  padding-top: 5px;
}

.secondary-preview-fade-enter-active,
.secondary-preview-fade-leave-active {
  transition:
    opacity calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-island);
}

.secondary-preview-fade-enter-from,
.secondary-preview-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

@keyframes hlIslandBar {
  from {
    transform: translateX(-100%);
  }
  to {
    transform: translateX(350%);
  }
}

@media (prefers-reduced-motion: reduce) {
  .hl-secondary-island.is-swapping-secondary {
    transform: none;
    transition: none;
  }

  .hl-secondary-preview__bar > i.is-slide {
    animation: none;
  }
}
</style>
