<script setup lang="ts">
/**
 * PrimaryIsland — 主岛呈现组件
 * 呈现当前主岛活动的模块标识、文案、进度条、动作按钮与展开折叠态。
 * 遵循收口契约：模块标识永远真实可见，不被通用 Spinner 掩盖，运行态由精细外圈进度环体现。
 */
import { computed } from 'vue'

import HlIcon from '@/components/ui/HlIcon.vue'
import { useI18n } from '@/locales'

import ActivityMark from './ActivityMark.vue'
import { resolvePresentation } from './presentationResolver'
import type { Activity } from './types'

const props = defineProps<{
  activity: Activity | null
  mode: 'idle' | 'notice' | 'task' | 'expanded'
  isPinned?: boolean
  leadSpin: boolean
  leadIcon: string
  leadToneClass: string
  showingPrice: boolean
  canDismissStatus?: boolean
  priceColor?: string
  headText: string
  headTextStyle: Record<string, string>
  headTruncated: boolean
  trailing: string
  crawlOnlyTask: boolean
  showBar: boolean
  taskPercent: number | null
  expandedShowsTasks: boolean
  taskLines: { key: string; text: string; percent: number | null }[]
  taskGoto?: string
  detailText?: string
  expandedIsTask: boolean
  canRetry: boolean
  goto?: string
  gotoLabel?: string
}>()

const emit = defineEmits<{
  (e: 'cancel'): void
  (e: 'retry'): void
  (e: 'dismiss'): void
  (e: 'goDetail'): void
  (e: 'goTask'): void
  (e: 'openTask'): void
  (e: 'return'): void
  (e: 'collapse'): void
}>()

const { t } = useI18n()

/** 模块视觉呈现解析（图标、徽章、动效环、色彩基调） */
const presentation = computed(() =>
  resolvePresentation(props.activity, {
    showingPrice: props.showingPrice,
    priceRunning: props.leadSpin,
  }),
)

/** 收起态右侧紧凑进度数值（若有） */
const compactPercent = computed<number | null>(() => {
  if (props.mode === 'expanded') return null
  if (props.taskPercent !== null) return props.taskPercent
  if (props.activity && props.activity.progress !== undefined && props.activity.progress !== null) {
    return props.activity.progress
  }
  return null
})

</script>

<template>
  <div class="hl-island__body" data-island-content>
    <!-- 头部行：四态共用一组元素，展开只换布局，不换实例 -->
    <div class="hl-island__row">
      <!-- 模块专属标识（进度加载动效完整嵌入 Logo 环，杜绝外部悬挂与通用覆盖） -->
      <ActivityMark
        class="hl-island__lead"
        :presentation="presentation"
        :progress="mode === 'expanded' ? taskPercent : activity?.progress"
        :image-url="activity?.iconImageUrl"
        :animation-key="activity?.id"
        :activity-id="activity?.id ?? (showingPrice ? '@price-status' : undefined)"
        :icon-color="showingPrice ? priceColor : undefined"
      />

      <!-- 标题文本 -->
      <span
        class="hl-island__text"
        :class="{ 'is-status': showingPrice }"
        :style="headTextStyle"
        :title="headTruncated ? headText : undefined"
      >
        {{ headText }}
      </span>

      <!-- 固定主岛标记 -->
      <span v-if="isPinned" class="hl-island__pinned" :title="t('island.pinned')">
        <HlIcon name="target" :size="11" />
      </span>

      <!-- 收起态百分比数值展示（右侧对齐） -->
      <span v-if="compactPercent !== null" class="hl-island__pct">
        {{ compactPercent }}%
      </span>

      <!-- 展开态尾部说明 -->
      <span v-if="trailing" class="hl-island__trail">{{ trailing }}</span>

      <button
        v-if="mode === 'expanded'"
        class="hl-island__slot-control"
        type="button"
        :title="t('island.returnToPrevious')"
        :aria-label="t('island.returnToPrevious')"
        @click.stop="emit('return')"
      >
        <HlIcon name="chevron-left" :size="12" />
      </button>
      <button
        v-if="mode === 'expanded'"
        class="hl-island__slot-control"
        type="button"
        :title="t('island.collapse')"
        :aria-label="t('island.collapse')"
        @click.stop="emit('collapse')"
      >
        <HlIcon name="chevron-down" :size="12" />
      </button>
      <button
        v-if="showingPrice && canDismissStatus"
        class="hl-island__slot-control"
        type="button"
        :title="t('island.hideStatus')"
        :aria-label="t('island.hideStatus')"
        @click.stop="emit('dismiss')"
      >
        <HlIcon name="close" :size="12" />
      </button>

      <!-- 快速取消按钮 -->
      <button v-if="crawlOnlyTask && mode === 'task'" class="hl-island__btn is-tiny" @click.stop="emit('cancel')">
        {{ t('island.task.cancel') }}
      </button>
    </div>

    <!-- 进度条：仅展开态呈现；收起态进度加载动效完整嵌入 Logo 图标环与右侧数值 -->
    <span
      v-if="mode === 'expanded' && showBar && taskPercent !== undefined"
      class="hl-island__bar"
      :class="{ 'is-slide': taskPercent === null }"
    >
      <i :style="taskPercent === null ? undefined : { width: `${taskPercent}%` }" />
    </span>

    <!-- 展开面：正文与动作分级落入 -->
    <Transition name="island-fold">
      <div v-if="mode === 'expanded'" class="hl-island__fold">
        <template v-if="expandedShowsTasks">
          <div v-if="taskLines.length" class="hl-island__tasks">
            <span v-for="task in taskLines" :key="task.key" class="hl-island__taskline">
              <span class="hl-island__tasktext">{{ task.text }}</span>
              <b v-if="task.percent !== null" class="hl-island__taskpct">{{ task.percent }}%</b>
            </span>
          </div>
          <span v-else class="hl-island__spacer" />
          <div v-if="taskGoto" class="hl-island__actions">
            <button class="hl-island__btn is-primary" @click.stop="emit('goTask')">
              {{ t('island.view') }}
            </button>
          </div>
          <span v-else class="hl-island__spacer" />
        </template>
        <template v-else>
          <p v-if="detailText" class="hl-island__detail">{{ detailText }}</p>
          <span v-else class="hl-island__spacer" />

          <div class="hl-island__actions">
            <template v-if="expandedIsTask">
              <button class="hl-island__btn is-primary" @click.stop="emit('openTask')">
                {{ t('island.task.open') }}
              </button>
              <button class="hl-island__btn" @click.stop="emit('cancel')">
                {{ t('island.task.cancel') }}
              </button>
            </template>
            <template v-else-if="showingPrice">
              <button class="hl-island__btn is-primary" @click.stop="emit('openTask')">
                {{ t('island.task.open') }}
              </button>
            </template>
            <template v-else>
              <button v-if="canRetry" class="hl-island__btn is-primary" @click.stop="emit('retry')">
                {{ t('island.retry') }}
              </button>
              <button
                v-if="goto"
                class="hl-island__btn"
                :class="{ 'is-primary': !canRetry }"
                @click.stop="emit('goDetail')"
              >
                {{ gotoLabel }}
              </button>
              <button class="hl-island__btn" @click.stop="emit('dismiss')">{{ t('island.dismiss') }}</button>
            </template>
          </div>
        </template>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.hl-island__pct {
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted);
  margin-left: auto;
  flex: 0 0 auto;
}
</style>
