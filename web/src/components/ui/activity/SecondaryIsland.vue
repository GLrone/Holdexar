<script setup lang="ts">
/**
 * SecondaryIsland — 副岛紧凑胶囊组件
 * 双任务时呈现为 36px 完整圆形模块徽标；三任务及以上延伸为 36px 高、至少 58px 宽的小胶囊。
 * 绝不用通用 Spinner 覆盖模块图标；支持悬停预览详情、点击余量进入总览面板、全套键盘快捷交互与空间切移动画。
 */
import { computed, onBeforeUnmount, ref } from 'vue'

import HlIcon from '@/components/ui/HlIcon.vue'
import { useI18n } from '@/locales'

import ActivityMark from './ActivityMark.vue'
import { resolvePresentation } from './presentationResolver'
import type { Activity } from './types'

const props = defineProps<{
  activity: Activity | null
  extraCount?: number
  isSwapHidden?: boolean
}>()

const emit = defineEmits<{
  (e: 'swap'): void
  (e: 'openOverview'): void
}>()

const { t } = useI18n()

const showPreview = ref(false)
let previewTimer: ReturnType<typeof setTimeout> | null = null

/** 模块视觉呈现解析 */
const presentation = computed(() => resolvePresentation(props.activity))

/** 提示文本 */
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
  previewTimer = setTimeout(() => {
    showPreview.value = false
  }, 120)
}

onBeforeUnmount(() => {
  if (previewTimer) clearTimeout(previewTimer)
})
</script>

<template>
  <div
    v-if="activity"
    class="hl-secondary-island"
    :class="[
      {
        'is-circle': !extraCount || extraCount <= 0,
        'is-capsule': extraCount && extraCount > 0,
        'is-swap-hidden': isSwapHidden,
      },
    ]"
    :data-activity-id="activity.id"
    :aria-hidden="isSwapHidden ? 'true' : undefined"
    :inert="isSwapHidden"
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
    <!-- 模块专属标识（进度加载动效完整嵌入 Logo 环，杜绝外部悬挂与通用覆盖） -->
    <ActivityMark
      class="hl-secondary-island__lead"
      :presentation="presentation"
      :progress="activity.progress"
      :image-url="activity.iconImageUrl"
      :animation-key="activity.id"
    />

    <!-- 余量计数徽章：仅 3 个及以上活动时呈现，点击直通活动总览面板 -->
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

        <div class="hl-secondary-preview__footer">
          <span class="hl-secondary-preview__tip">{{ t('island.previewHint') }}</span>
          <button
            type="button"
            class="hl-secondary-preview__hub-btn"
            @click.stop="emit('openOverview')"
          >
            {{ t('island.hub') }}
          </button>
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
  height: 36px;
  background: var(--island-bg);
  box-shadow: var(--shadow-island);
  color: var(--text-primary);
  border: none;
  outline: none;
  cursor: pointer;
  user-select: none;
  box-sizing: border-box;
  transition:
    width calc(var(--duration-3) * var(--motion-scale)) var(--ease-island),
    transform calc(var(--duration-3) * var(--motion-scale)) var(--ease-island),
    box-shadow calc(var(--duration-3) * var(--motion-scale)) var(--ease-out),
    opacity calc(var(--duration-3) * var(--motion-scale)) var(--ease-out);
}

/* 双活动收起态：完整的圆形，直径 36px，居中对齐 */
.hl-secondary-island.is-circle {
  width: 36px;
  border-radius: 18px;
  padding: 0;
  justify-content: center;
}

/* 三个及以上活动：适度延展为 36px 高、至少 58px 宽的小胶囊 */
.hl-secondary-island.is-capsule {
  min-width: 58px;
  border-radius: 18px;
  padding: 0 8px;
  justify-content: space-between;
  gap: 6px;
}

.hl-secondary-island:hover {
  transform: scale(1.04);
}

.hl-secondary-island:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.hl-secondary-island.is-swap-hidden {
  visibility: hidden;
  pointer-events: none;
}

.hl-secondary-island__count {
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted);
  padding: 1px 5px;
  border-radius: 9px;
  background: var(--island-btn);
  transition: background var(--transition), color var(--transition);
}

.hl-secondary-island__count:hover {
  background: var(--accent);
  color: var(--on-accent-fill);
}

.hl-secondary-preview {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 230px;
  padding: 10px 12px;
  border-radius: 12px;
  background: var(--island-bg);
  box-shadow: var(--shadow-island);
  border: 1px solid var(--border-soft);
  color: var(--text-primary);
  display: flex;
  flex-direction: column;
  gap: 7px;
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
  font-variant-numeric: tabular-nums;
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

.hl-secondary-preview__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  border-top: 1px solid var(--border-soft);
  padding-top: 6px;
}

.hl-secondary-preview__tip {
  font-size: 10px;
  color: var(--text-muted);
  flex: 1 1 auto;
}

.hl-secondary-preview__hub-btn {
  border: none;
  background: var(--island-btn);
  color: var(--text-primary);
  font-size: 10px;
  font-weight: 500;
  padding: 2px 6px;
  border-radius: 4px;
  cursor: pointer;
  flex: 0 0 auto;
}

.hl-secondary-preview__hub-btn:hover {
  background: var(--accent);
  color: var(--on-accent-fill);
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
  .hl-secondary-preview__bar > i.is-slide {
    animation: none;
  }
}
</style>
