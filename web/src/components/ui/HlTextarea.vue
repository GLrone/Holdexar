<script setup lang="ts">
const model = defineModel<string>({ default: '' })

defineProps<{
  placeholder?: string
  rows?: number
  disabled?: boolean
}>()
</script>

<template>
  <textarea
    v-model="model"
    class="hl-textarea"
    :placeholder="placeholder"
    :rows="rows ?? 3"
    :disabled="disabled"
  />
</template>
