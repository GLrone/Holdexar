<script setup lang="ts">
import { computed } from 'vue'

import { normalizeAvatarUrl } from '@/api/avatar'

const props = withDefaults(
  defineProps<{
    /** 头像图 URL；缺省时显示 name 首字符（URL 经统一出口归一） */
    src?: string
    /** 名称（取首字符做占位头像） */
    name?: string
    size?: 'sm' | 'md' | 'lg'
    square?: boolean
  }>(),
  { src: '', name: '', size: 'md', square: false },
)

const initial = computed(() => props.name.trim().charAt(0).toUpperCase())
const resolvedSrc = computed(() => normalizeAvatarUrl(props.src))
</script>

<template>
  <span
    class="hl-avatar"
    :class="[
      size === 'sm' ? 'hl-avatar--sm' : '',
      size === 'lg' ? 'hl-avatar--lg' : '',
      square ? 'hl-avatar--square' : '',
    ]"
  >
    <!-- 失败自动换 hlretry query 重试 + 全败落首字符占位（assetCache 体系） -->
    <HlImg v-if="resolvedSrc" :src="resolvedSrc" :alt="name || 'avatar'">
      <template #fallback>{{ initial || '?' }}</template>
    </HlImg>
    <template v-else>{{ initial || '?' }}</template>
  </span>
</template>
