<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 步骤标题序列 */
    steps: string[]
    /** 当前步骤（0 基） */
    active: number
  }>(),
  { steps: () => [], active: 0 },
)

const states = computed(() =>
  props.steps.map((_, i) => (i < props.active ? 'finish' : i === props.active ? 'process' : 'wait')),
)
</script>

<template>
  <div class="hl-steps">
    <div v-for="(title, i) in steps" :key="i" class="hl-step" :class="`is-${states[i]}`">
      <span class="hl-step__head">{{ states[i] === 'finish' ? '✓' : i + 1 }}</span>
      <span v-if="i < steps.length - 1" class="hl-step__line" />
      <span class="hl-step__title">{{ title }}</span>
    </div>
  </div>
</template>
