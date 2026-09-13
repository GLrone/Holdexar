<script setup lang="ts">
withDefaults(
  defineProps<{
    label?: string
    /** 史低变体：hlnew = 新史低蓝勾 / hleq = 平史低绿勾 */
    variant?: 'default' | 'hlnew' | 'hleq'
    disabled?: boolean
  }>(),
  { label: '', variant: 'default', disabled: false },
)

const model = defineModel<boolean>({ default: false })
</script>

<template>
  <label
    class="hl-checkbox"
    :class="[
      { 'is-on': model },
      variant !== 'default' ? `hl-checkbox--${variant}` : '',
    ]"
    :style="disabled ? 'opacity:.5;pointer-events:none' : ''"
  >
    <input v-model="model" type="checkbox" style="display: none" />
    <span class="hl-checkbox__box" />
    <span v-if="label || $slots.default" class="hl-checkbox__label">
      <slot>{{ label }}</slot>
    </span>
  </label>
</template>
