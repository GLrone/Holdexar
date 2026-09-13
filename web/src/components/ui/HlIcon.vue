<script setup lang="ts">
import { computed } from 'vue'
import { ICONS, isIconName } from './icons'

const props = defineProps<{
  /** 认可清单中的图标名，见 ui/icons.ts */
  name: string
  /** px 尺寸，默认 14 */
  size?: number
}>()

const content = computed(() => (isIconName(props.name) ? ICONS[props.name] : ''))
</script>

<template>
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    :width="size ?? 14"
    :height="size ?? 14"
    aria-hidden="true"
    v-html="content"
  />
</template>
