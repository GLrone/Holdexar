<script setup lang="ts">
import { computed } from 'vue'

import HlIcon from '@/components/ui/HlIcon.vue'
import HlImg from '@/components/ui/HlImg.vue'
import { useI18n } from '@/locales'

import type { ActivityPresentation } from './presentationResolver'

const props = withDefaults(defineProps<{
  presentation: ActivityPresentation
  progress?: number | null
  imageUrl?: string
  animationKey?: string
  activityId?: string
  iconSize?: number
  iconColor?: string
}>(), {
  iconSize: 12,
})

const { t } = useI18n()

const clampedProgress = computed<number | null>(() => {
  if (props.progress === undefined || props.progress === null) return null
  return Math.min(100, Math.max(0, props.progress))
})

const orbitStyle = computed(() => {
  if (clampedProgress.value === null) return undefined
  const filled = (59.69 * clampedProgress.value) / 100
  return { strokeDasharray: `${filled.toFixed(2)} 59.69`, strokeDashoffset: '0' }
})

const markKey = computed(() => {
  if (props.imageUrl) return `${props.animationKey ?? ''}:image:${props.imageUrl}`
  if (props.presentation.badgeType === 'badge') {
    return `${props.animationKey ?? ''}:badge:${props.presentation.badgeKey ?? props.presentation.badgeText ?? ''}`
  }
  return `${props.animationKey ?? ''}:icon:${props.presentation.iconName}`
})

const badgeText = computed(() => (
  props.presentation.badgeKey
    ? t(props.presentation.badgeKey)
    : (props.presentation.badgeText ?? '')
))
</script>

<template>
  <span
    class="hl-activity-mark"
    :class="[`is-${presentation.tone}`, `is-motion-${presentation.motion}`]"
    :style="iconColor ? { color: iconColor } : undefined"
    :data-activity-logo="activityId"
    aria-hidden="true"
  >
    <svg v-if="presentation.motion === 'orbit'" class="hl-activity-mark__orbit" viewBox="0 0 24 24">
      <circle
        class="hl-activity-mark__orbit-active"
        cx="12"
        cy="12"
        r="9.5"
        :class="{ 'is-determinate': clampedProgress !== null, 'is-indeterminate': clampedProgress === null }"
        :style="orbitStyle"
      />
    </svg>

    <Transition name="island-icon-switch" mode="out-in">
      <span :key="markKey" class="hl-activity-mark__glyph">
        <HlImg
          v-if="imageUrl"
          :src="imageUrl"
          alt=""
          class="hl-activity-mark__image"
          draggable="false"
        >
          <template #fallback>
            <HlIcon :name="presentation.iconName" :size="iconSize" class="hl-activity-mark__icon" />
          </template>
        </HlImg>
        <span v-else-if="presentation.badgeType === 'badge'" class="hl-activity-mark__badge">
          {{ badgeText }}
        </span>
        <HlIcon
          v-else
          :name="presentation.iconName"
          :size="iconSize"
          class="hl-activity-mark__icon"
        />
      </span>
    </Transition>

    <span v-if="presentation.motion === 'dot'" class="hl-activity-mark__dot" />
    <span v-if="presentation.hasWarningMarker" class="hl-activity-mark__warn-dot">!</span>
  </span>
</template>

<style scoped>
.hl-activity-mark {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex: 0 0 22px;
  color: var(--text-muted);
}

.hl-activity-mark.is-motion-pulse {
  animation: hl-mark-pulse 2s ease-in-out infinite;
}

@keyframes hl-mark-pulse {
  0%,
  100% { opacity: 1; }
  50% { opacity: 0.78; }
}

.hl-activity-mark__orbit {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  transform: rotate(-90deg);
  pointer-events: none;
}

.hl-activity-mark__orbit-active {
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  transform-box: view-box;
  transform-origin: 50% 50%;
  transition: stroke-dasharray calc(var(--duration-3) * var(--motion-scale)) var(--ease-out);
}

.hl-activity-mark__orbit-active.is-indeterminate {
  stroke-dasharray: 18 42;
  animation: hl-mark-orbit 1.2s linear infinite;
}

@keyframes hl-mark-orbit {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.hl-activity-mark.is-success { color: var(--success); }
.hl-activity-mark.is-error { color: var(--danger); }
.hl-activity-mark.is-warning { color: var(--warning); }
.hl-activity-mark.is-progress,
.hl-activity-mark.is-accent { color: var(--accent); }

.hl-activity-mark__glyph {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
}

.hl-activity-mark__image {
  display: block;
  width: 19px;
  height: 19px;
  max-width: none;
  border: 0;
  border-radius: 5px;
  outline: 0;
  box-shadow: none;
  background: transparent;
  object-fit: cover;
}

.hl-activity-mark__icon {
  color: currentColor;
}

.hl-activity-mark__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--accent);
  color: var(--on-accent-fill);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
}

.hl-activity-mark__dot {
  position: absolute;
  top: 1px;
  right: 1px;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--success);
}

.hl-activity-mark__warn-dot {
  position: absolute;
  top: -2px;
  right: -2px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--danger);
  color: var(--on-accent-fill);
  font-size: 8px;
  font-weight: 700;
  line-height: 1;
}

.island-icon-switch-enter-active,
.island-icon-switch-leave-active {
  transition:
    opacity calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.island-icon-switch-enter-from {
  opacity: 0;
  transform: translateY(3px);
}

.island-icon-switch-leave-to {
  opacity: 0;
  transform: translateY(-3px);
}

@media (prefers-reduced-motion: reduce) {
  .hl-activity-mark.is-motion-pulse,
  .hl-activity-mark__orbit-active.is-indeterminate {
    animation: none;
  }
}
</style>
