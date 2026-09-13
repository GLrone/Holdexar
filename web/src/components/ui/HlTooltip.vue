<script setup lang="ts">
withDefaults(
  defineProps<{
    content: string
    dark?: boolean
    wide?: boolean
    /** 弹出方位：top=宿主上方（默认）/ bottom=宿主下方（宿主贴屏幕顶的场景） */
    placement?: 'top' | 'bottom'
    /** 水平对齐：center=居中（默认）/ end=右缘对齐（宿主贴屏幕右缘的场景） */
    align?: 'center' | 'end'
    /** 富内容气泡（封面缩略图等自定义结构走 #popper 插槽）：
        rich 关闭 nowrap/默认内边距，插槽宽度交由内容决定 */
    rich?: boolean
  }>(),
  { content: '', dark: true, wide: false, placement: 'top', align: 'center', rich: false },
)
</script>

<template>
  <span class="hl-tip-host">
    <slot />
    <span
      class="hl-popper"
      :class="{
        'is-dark': dark,
        'is-wide': wide,
        'is-rich': rich,
        'is-bottom': placement === 'bottom',
        'is-end': align === 'end',
      }"
    >
      <slot name="popper">{{ content }}</slot>
    </span>
  </span>
</template>
