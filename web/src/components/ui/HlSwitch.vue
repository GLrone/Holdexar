<script setup lang="ts">
withDefaults(
  defineProps<{
    label?: string
    /** 选中态使用 accent 蓝而非 success 绿 */
    accent?: boolean
    disabled?: boolean
  }>(),
  { label: '', accent: false, disabled: false },
)

const model = defineModel<boolean>({ default: false })
</script>

<template>
  <span
    class="hl-switch"
    :class="[{ 'is-on': model }, accent ? 'hl-switch--accent' : '']"
    :style="disabled ? 'opacity:.5;pointer-events:none' : ''"
    role="switch"
    :aria-checked="model"
    @click="model = !model"
  >
    <span class="hl-switch__core" />
    <span v-if="label" class="hl-switch__label">{{ label }}</span>
  </span>
</template>
