<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    min?: number
    max?: number
    step?: number
    disabled?: boolean
  }>(),
  { min: 0, max: 100, step: 1, disabled: false },
)

const model = defineModel<number>({ default: 0 })

const clamped = computed(() => Math.min(props.max, Math.max(props.min, model.value)))

function stepBy(delta: number) {
  const next = clamped.value + delta * props.step
  model.value = Math.min(props.max, Math.max(props.min, Math.round(next * 1000) / 1000))
}
</script>

<template>
  <span class="hl-input-number">
    <button
      type="button"
      class="hl-input-number__btn hl-input-number__btn--dec"
      :disabled="disabled"
      @click="stepBy(-1)"
    >
      −
    </button>
    <span class="hl-input-wrap">
      <input
        v-model.number="model"
        type="number"
        :min="min"
        :max="max"
        :step="step"
        :disabled="disabled"
        style="text-align: center"
        @blur="model = clamped"
      />
    </span>
    <button
      type="button"
      class="hl-input-number__btn hl-input-number__btn--inc"
      :disabled="disabled"
      @click="stepBy(1)"
    >
      +
    </button>
  </span>
</template>
