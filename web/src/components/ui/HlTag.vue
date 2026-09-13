<script setup lang="ts">
withDefaults(
  defineProps<{
    type?: 'default' | 'accent' | 'success' | 'danger' | 'warning' | 'info'
    closable?: boolean
  }>(),
  { type: 'default', closable: false },
)

const emit = defineEmits<{ close: [] }>()
</script>

<template>
  <span class="hl-tag" :class="type !== 'default' ? `hl-tag--${type}` : ''">
    <slot />
    <i v-if="closable" class="hl-tag__close" @click.stop="emit('close')">✕</i>
  </span>
</template>
