<script setup lang="ts">
import { ref } from 'vue'

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
    /** 溢出容器内使用：popper 改 fixed 定位，悬停时按宿主矩形现场换算坐标。
        场景：el-table 单元格 / 滚动容器会把 absolute 气泡整体裁掉
        （overflow:hidden 对 fixed 后代不生效），fixed 是唯一逃逸通道；
        前提是宿主到根之间没有 transform/filter 祖先（会成为包含块）。 */
    fixed?: boolean
  }>(),
  {
    content: '',
    dark: true,
    wide: false,
    placement: 'top',
    align: 'center',
    rich: false,
    fixed: false,
  },
)

const host = ref<HTMLElement | null>(null)
const pos = ref<{ left: number; top: number } | null>(null)

/** 悬停瞬间按宿主当前矩形定位（fixed 坐标系 = 视口）；滚动后下次悬停重算 */
function syncPos(r: DOMRect, placement: 'top' | 'bottom') {
  pos.value = {
    left: r.left + r.width / 2,
    top: placement === 'bottom' ? r.bottom + 8 : r.top - 8,
  }
}

function onEnter(placement: 'top' | 'bottom') {
  if (host.value) syncPos(host.value.getBoundingClientRect(), placement)
}
</script>

<template>
  <span
    ref="host"
    class="hl-tip-host"
    @mouseenter="fixed && onEnter(placement)"
  >
    <slot />
    <!-- 空内容不弹泡：content 动态计算（如实时换算提示）为空时气泡整体消失，
         而不是悬停出一颗空壳。#popper 插槽存在即视为有内容 -->
    <span
      v-show="content || $slots.popper"
      class="hl-popper"
      :class="{
        'is-dark': dark,
        'is-wide': wide,
        'is-rich': rich,
        'is-bottom': placement === 'bottom',
        'is-end': align === 'end',
        'is-fixed': fixed,
      }"
      :style="fixed && pos ? { left: `${pos.left}px`, top: `${pos.top}px` } : undefined"
    >
      <slot name="popper">{{ content }}</slot>
    </span>
  </span>
</template>
