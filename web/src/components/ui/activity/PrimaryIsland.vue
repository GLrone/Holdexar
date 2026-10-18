<script setup lang="ts">
/**
 * PrimaryIsland — 主岛呈现组件
 * 呈现当前主岛活动的图标、文案、进度条、动作按钮与展开折叠态。
 */
import HlIcon from '@/components/ui/HlIcon.vue'
import HlSpinner from '@/components/ui/HlSpinner.vue'
import { useI18n } from '@/locales'
import type { Activity } from './types'

defineProps<{
  activity: Activity | null
  mode: 'idle' | 'notice' | 'task' | 'expanded'
  leadSpin: boolean
  leadIcon: string
  leadToneClass: string
  showingPrice: boolean
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
  (e: 'mute'): void
  (e: 'goDetail'): void
  (e: 'goTask'): void
  (e: 'openTask'): void
}>()

const { t } = useI18n()
</script>

<template>
  <div class="hl-island__body">
    <!-- 头部行：四态共用一组元素，展开只换布局，不换实例 -->
    <div class="hl-island__row">
      <span class="hl-island__lead">
        <HlSpinner v-if="leadSpin" class="hl-island__spin" />
        <HlIcon
          v-else
          :name="leadIcon"
          :size="13"
          class="hl-island__icon"
          :class="leadToneClass"
          :style="showingPrice ? { color: priceColor } : undefined"
        />
      </span>
      <span
        class="hl-island__text"
        :class="{ 'is-status': showingPrice }"
        :style="headTextStyle"
        :title="headTruncated ? headText : undefined"
      >
        {{ headText }}
      </span>
      <span v-if="trailing" class="hl-island__trail">{{ trailing }}</span>
      <button v-if="crawlOnlyTask && mode === 'task'" class="hl-island__btn is-tiny" @click.stop="emit('cancel')">
        {{ t('island.task.cancel') }}
      </button>
    </div>

    <!-- 进度条：任务态常显，展开中的任务同样保留 -->
    <span v-if="showBar" class="hl-island__bar" :class="{ 'is-slide': taskPercent === null }">
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
              <button class="hl-island__link" @click.stop="emit('mute')">{{ t('island.mute') }}</button>
            </template>
          </div>
        </template>
      </div>
    </Transition>
  </div>
</template>
