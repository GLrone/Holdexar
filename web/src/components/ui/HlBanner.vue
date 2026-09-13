<script setup lang="ts">
import { computed } from 'vue'

export interface HlBannerItem {
  /** 支持 <b> 加价 / <span class="new"> 高亮 */
  html: string
}

const props = withDefaults(
  defineProps<{
    items: HlBannerItem[]
    /** 滚动速度：slow=40s / fast=10s / 默认 22s */
    speed?: 'slow' | 'normal' | 'fast'
  }>(),
  { speed: 'normal' },
)

/** 无缝滚动：内容渲染两遍，位移 -50% */
const doubled = computed(() => [...props.items, ...props.items])
</script>

<template>
  <div class="hl-banner" :class="{ slow: speed === 'slow', fast: speed === 'fast' }">
    <div class="hl-banner__mask">
      <div class="hl-banner__track">
        <span v-for="(item, i) in doubled" :key="i" class="hl-banner__item" v-html="item.html" />
      </div>
    </div>
  </div>
</template>
