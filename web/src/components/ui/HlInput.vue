<script setup lang="ts">
import HlIcon from './HlIcon.vue'

const model = defineModel<string>({ default: '' })

defineProps<{
  placeholder?: string
  /** 前缀图标名（ui/icons.ts 认可清单） */
  prefixIcon?: string
  /** 后缀图标名 */
  suffixIcon?: string
  disabled?: boolean
  type?: string
}>()
</script>

<template>
  <span class="hl-input-wrap" :class="{ 'is-focus': undefined }">
    <span v-if="prefixIcon" class="hl-iprefix"><HlIcon :name="prefixIcon" /></span>
    <input
      v-model="model"
      :type="type ?? 'text'"
      :placeholder="placeholder"
      :disabled="disabled"
    />
    <span v-if="suffixIcon" class="hl-isuffix"><HlIcon :name="suffixIcon" /></span>
  </span>
</template>
