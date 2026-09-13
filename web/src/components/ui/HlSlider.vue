<script setup lang="ts">
import { computed, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    min?: number
    max?: number
    showValue?: boolean
  }>(),
  { min: 0, max: 100, showValue: true },
)

const model = defineModel<number>({ default: 0 })

const track = ref<HTMLElement | null>(null)
const dragging = ref(false)

const pct = computed(
  () => ((model.value - props.min) / (props.max - props.min)) * 100,
)

function apply(clientX: number) {
  const el = track.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
  model.value = Math.round(props.min + ratio * (props.max - props.min))
}

function onDown(e: PointerEvent) {
  dragging.value = true
  apply(e.clientX)
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

function onMove(e: PointerEvent) {
  if (dragging.value) apply(e.clientX)
}

function onUp() {
  dragging.value = false
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
}
</script>

<template>
  <span class="hl-slider" @pointerdown.prevent="onDown">
    <span ref="track" class="hl-slider__track">
      <span class="hl-slider__bar" :style="{ width: pct + '%' }" />
      <span class="hl-slider__thumb" :style="{ left: pct + '%' }" />
    </span>
    <span v-if="showValue" class="hl-slider__val">{{ model }}</span>
  </span>
</template>
