<script setup lang="ts">
import type { HlSelectOption } from './HlSelect.vue'

withDefaults(defineProps<{ options: HlSelectOption[] }>(), { options: () => [] })

const model = defineModel<string | number>({ default: '' })
</script>

<template>
  <span class="hl-radio-group">
    <button
      v-for="opt in options"
      :key="opt.value"
      type="button"
      class="hl-radio-btn"
      :class="{ 'is-on': model === opt.value }"
      @click="model = opt.value"
    >
      {{ opt.label }}
    </button>
  </span>
</template>
