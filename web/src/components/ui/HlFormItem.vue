<script setup lang="ts">
defineProps<{
  label?: string
  /** 底部提示文字；error 存在时优先显示 error 并标红 */
  hint?: string
  error?: string
  required?: boolean
}>()
</script>

<template>
  <div class="hl-form-item" :class="{ 'is-error': !!error }">
    <label v-if="label" class="hl-form-label">
      {{ label }}<span v-if="required" style="color: var(--danger)">&nbsp;*</span>
    </label>
    <slot />
    <span v-if="error || hint" class="hl-form-hint">{{ error || hint }}</span>
  </div>
</template>
