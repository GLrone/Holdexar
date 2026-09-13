<script setup lang="ts">
import { useI18n } from '@/locales'

/**
 * 空态。
 *
 * **`text` 的默认值刻意不写在 `withDefaults` 里**：那里的默认值只在
 * `defineProps` 求值那一刻算一次，写 `t('common.empty')` 会把语言**冻结**在
 * 组件创建时的选择——切语言后已挂载的空态不会变，而且因为它是模块级单例式的
 * 静态默认值，重挂载也未必能救回来。放进模板的 `??` 才在每次渲染时求值，
 * 天然跟随 locale store。`icon` 是无语言含义的字符，留在这里没问题。
 *
 * **两个尺寸**：`md` 是页面级空态（整块区域没有内容），`sm` 是「表格/列表没有行」
 * 的小占位（嵌在卡片内部，此前靠行内 `style="padding: 16px 0"` 打补丁，全项目 9 处）。
 * 传 `icon=""` 则不渲染图标——`sm` 档本来就该无图标，一个 40px 的 emoji 塞进表格
 * 下方比没有图标更糟。默认图标只在 `md` 且未显式传 `icon` 时出现。
 */
withDefaults(
  defineProps<{
    /** emoji / 字符图标；传空串则不渲染图标（`sm` 档的常规用法） */
    icon?: string
    text?: string
    /** md=页面级空态 · sm=表格/列表内的小占位 */
    size?: 'md' | 'sm'
  }>(),
  { icon: '📦', size: 'md' },
)

const { t } = useI18n()
</script>

<template>
  <div class="hl-empty" :class="`hl-empty--${size}`">
    <span v-if="icon" class="hl-empty__icon">{{ icon }}</span>
    <span class="hl-empty__text"><slot>{{ text ?? t('common.empty') }}</slot></span>
  </div>
</template>
