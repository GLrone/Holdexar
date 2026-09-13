<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 0-100 */
    value: number
    tone?: 'default' | 'success' | 'danger' | 'warning'
    showText?: boolean
  }>(),
  { tone: 'default', showText: true },
)

const width = computed(() => Math.min(100, Math.max(0, props.value)))
</script>

<template>
  <div
    class="hl-progress"
    :class="tone !== 'default' ? `hl-progress--${tone}` : ''"
  >
    <div class="hl-progress__bar">
      <div class="hl-progress__outer">
        <div class="hl-progress__inner" :style="{ width: width + '%' }" />
      </div>
    </div>
    <span v-if="showText" class="hl-progress__txt">{{ width }}%</span>
  </div>
</template>
