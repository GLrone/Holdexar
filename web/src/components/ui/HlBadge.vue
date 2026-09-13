<script setup lang="ts">
withDefaults(
  defineProps<{
    /** 数字角标；0 或负数不显示 */
    count?: number
    /** 小红点模式（优先于 count） */
    dot?: boolean
  }>(),
  { count: 0, dot: false },
)
</script>

<template>
  <span class="hl-badge">
    <slot />
    <span v-if="dot" class="hl-badge__dot" />
    <span v-else-if="count > 0" class="hl-badge__num">{{ count > 99 ? '99+' : count }}</span>
  </span>
</template>
