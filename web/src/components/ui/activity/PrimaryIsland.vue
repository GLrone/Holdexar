<script setup lang="ts">
/**
 * PrimaryIsland — 主岛呈现组件
 * 呈现当前主岛活动的模块标识、文案、进度条与动作按钮，紧凑与展开共用同一实例结构。
 * 动作按钮只由活动数据能力决定（retry/cancel/to），新增信息来源零视图改动。
 */
import { computed } from 'vue'

import HlIcon from '@/components/ui/HlIcon.vue'
import { useI18n } from '@/locales'

import ActivityMark from './ActivityMark.vue'
import { resolveActivityCardView, resolvePresentation } from './presentationResolver'
import type { Activity } from './types'

const props = defineProps<{
  activity: Activity | null
  mode: 'idle' | 'notice' | 'task' | 'expanded'
  isPinned?: boolean
  priceRunning: boolean
  showingPrice: boolean
  canDismissStatus?: boolean
  priceColor?: string
  headText: string
  headTextStyle: Record<string, string>
  headTruncated: boolean
  trailing?: string
  detailText?: string
}>()

const emit = defineEmits<{
  (e: 'cancel'): void
  (e: 'retry'): void
  (e: 'dismiss'): void
  (e: 'goDetail'): void
  (e: 'collapse'): void
}>()

const { t } = useI18n()

/** 模块视觉呈现解析（图标、徽章、动效环、色彩基调） */
const presentation = computed(() =>
  resolvePresentation(props.activity, {
    showingPrice: props.showingPrice,
    priceRunning: props.priceRunning,
  }),
)

/** 动作与进度统一派生（与 HlIsland 处理器共用同一套规则） */
const view = computed(() =>
  resolveActivityCardView(props.activity, { showingPrice: props.showingPrice }),
)

const targetLabel = computed(() => {
  if (view.value.targetLabelKind === 'task') return t('island.task.open')
  if (view.value.targetLabelKind === 'logs') return t('island.viewLogs')
  return props.activity?.toLabel || t('island.view')
})

/** 收起态右侧紧凑进度数值（若有） */
const compactPercent = computed(() =>
  props.mode !== 'expanded' && view.value.percent !== null ? view.value.percent : null,
)

</script>

<template>
  <div class="hl-island__body" data-island-content>
    <!-- 头部行：四态共用一组元素，展开只换布局，不换实例 -->
    <div class="hl-island__row">
      <!-- 模块专属标识（进度加载动效完整嵌入 Logo 环，杜绝外部悬挂与通用覆盖） -->
      <ActivityMark
        class="hl-island__lead"
        :presentation="presentation"
        :progress="mode === 'expanded' ? view.percent : activity?.progress"
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
        v-if="mode === 'expanded' && view.dismissible"
        class="hl-island__slot-control"
        type="button"
        :title="t('island.dismiss')"
        :aria-label="t('island.dismiss')"
        @click.stop="emit('dismiss')"
      >
        <HlIcon name="close" :size="12" />
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

      <!-- 收起态快速取消（任务运行中且自带取消能力时呈现） -->
      <button
        v-if="mode !== 'expanded' && view.cancelable"
        class="hl-island__btn is-tiny"
        @click.stop="emit('cancel')"
      >
        {{ t('island.task.cancel') }}
      </button>
    </div>

    <!-- 进度条：仅展开态呈现；收起态进度加载动效完整嵌入 Logo 图标环与右侧数值 -->
    <span
      v-if="mode === 'expanded' && view.busy"
      class="hl-island__bar"
      :class="{ 'is-slide': view.percent === null }"
    >
      <i :style="view.percent === null ? undefined : { width: `${view.percent}%` }" />
    </span>

    <!-- 展开面：正文与动作按能力落入 -->
    <Transition name="island-fold">
      <div v-if="mode === 'expanded'" class="hl-island__fold">
        <p v-if="detailText" class="hl-island__detail">{{ detailText }}</p>

        <div class="hl-island__actions">
          <button v-if="view.canRetry" class="hl-island__btn is-primary" @click.stop="emit('retry')">
            {{ t('island.retry') }}
          </button>
          <button
            v-if="view.target"
            class="hl-island__btn"
            :class="{ 'is-primary': !view.canRetry }"
            @click.stop="emit('goDetail')"
          >
            {{ targetLabel }}
          </button>
          <button
            v-if="view.cancelable"
            class="hl-island__btn"
            @click.stop="emit('cancel')"
          >
            {{ t('island.task.cancel') }}
          </button>
        </div>
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
