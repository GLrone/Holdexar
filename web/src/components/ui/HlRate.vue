<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{ max?: number }>(), { max: 5 })

const model = defineModel<number>({ default: 0 })

const stars = computed(() => Array.from({ length: props.max }, (_, i) => i + 1))
</script>

<template>
  <span class="hl-rate" role="radiogroup">
    <span
      v-for="s in stars"
      :key="s"
      class="hl-rate__star"
      :class="{ 'is-on': s <= model }"
      @click="model = s"
    >
      ★
    </span>
  </span>
</template>
