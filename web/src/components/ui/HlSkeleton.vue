<script setup lang="ts">
/**
 * 骨架屏。
 *
 * 存在的意义是把项目里 9 处「加载中…」纯文字（其中 bills 三个 tab 是**加载中什么都不
 * 显示**的白屏）换成「提前占位」：骨架屏的尺寸必须与真实内容接近，数据到达时布局才
 * 不跳。所以组合形态里的间距一律取 `--space-*`、封面高度与 `lib-cover` 对齐。
 *
 * 原先只有 `line` / `circle` 两种单条形态，没有任何调用方；`text` / `card` 是本次
 * 按实际加载场景补的。
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** line=单条横线（默认） / circle=圆 / title=标题行 / text=多行段落 / card=卡片（封面+两行） */
    variant?: 'line' | 'circle' | 'title' | 'text' | 'card'
    /** 段落行数（variant="text"）。末行自动收窄，避免「四根等长横条」看着像表格 */
    rows?: number
    /** 重复份数（卡片网格 / 列表多条） */
    count?: number
    /** 段落左侧挂一枚圆形头像骨（成员列表场景） */
    avatar?: boolean
    width?: string
    height?: string
  }>(),
  { variant: 'line', rows: 3, count: 1, avatar: false, width: '', height: '' },
)

const style = computed(() => ({
  width: props.width || undefined,
  height: props.height || undefined,
}))

const repeat = computed(() => Math.max(1, props.count))
const rowCount = computed(() => Math.max(1, props.rows))
/** 末行 60%：等长横条看着像表格，不像文字 */
const rowWidth = (i: number) => (i === rowCount.value - 1 && rowCount.value > 1 ? '60%' : '100%')
</script>

<template>
  <div v-if="variant === 'text'" class="hl-skel-group" role="status" aria-busy="true">
    <div v-for="n in repeat" :key="n" class="hl-skel-text" :class="{ 'has-avatar': avatar }">
      <span v-if="avatar" class="hl-skeleton hl-skeleton--circle hl-skel-text__ava" />
      <span class="hl-skel-text__rows">
        <span v-for="i in rowCount" :key="i" class="hl-skeleton" :style="{ width: rowWidth(i - 1) }" />
      </span>
    </div>
  </div>

  <div v-else-if="variant === 'card'" class="hl-skel-cards" role="status" aria-busy="true">
    <div v-for="n in repeat" :key="n" class="hl-skel-card">
      <span class="hl-skeleton hl-skel-card__cover" />
      <span class="hl-skeleton hl-skel-card__line" style="width: 72%" />
      <span class="hl-skeleton hl-skel-card__line" style="width: 46%" />
    </div>
  </div>

  <span
    v-else
    class="hl-skeleton"
    :class="{ 'hl-skeleton--circle': variant === 'circle', 'hl-skeleton--title': variant === 'title' }"
    :style="style"
    role="status"
    aria-busy="true"
  />
</template>

<style scoped>
.hl-skel-group { display: flex; flex-direction: column; gap: var(--space-5); }
.hl-skel-text { display: flex; align-items: flex-start; gap: var(--space-3); }
.hl-skel-text__ava { width: 32px; height: 32px; flex-shrink: 0; }
.hl-skel-text__rows {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.hl-skel-text__rows .hl-skeleton { height: 11px; }

/* 卡片网格与 family / 家庭库的 .lib-grid 同格宽，落位不跳 */
.hl-skel-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(165px, 1fr));
  gap: var(--space-4);
}
.hl-skel-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  background: var(--surface-inset);
  border: 1px solid var(--line-1);
  border-radius: var(--radius);
  overflow: hidden;
  padding-bottom: var(--space-3);
}
.hl-skel-card__cover { height: 78px; border-radius: 0; flex-shrink: 0; }
.hl-skel-card__line { height: 11px; margin: 0 var(--space-4); }
</style>
