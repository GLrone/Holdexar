<script setup lang="ts">
import { computed } from 'vue'
import HlIcon from './HlIcon.vue'

const props = withDefaults(
  defineProps<{
    type?: 'success' | 'warning' | 'info' | 'error'
    title?: string
    desc?: string
    closable?: boolean
  }>(),
  { type: 'info', title: '', desc: '', closable: false },
)

const emit = defineEmits<{ close: [] }>()

const ICON = computed(
  () =>
    ({ success: 'check-circle', warning: 'warning', info: 'info', error: 'x-circle' })[
      props.type
    ] ?? 'info',
)
</script>

<template>
  <div class="hl-alert" :class="`hl-alert--${type}`">
    <span class="hl-alert__icon"><HlIcon :name="ICON" /></span>
    <div>
      <div v-if="title || $slots.title" class="hl-alert__title"><slot name="title">{{ title }}</slot></div>
      <div v-if="desc || $slots.default" class="hl-alert__desc"><slot>{{ desc }}</slot></div>
    </div>
    <button v-if="closable" type="button" class="hl-alert__close" @click="emit('close')">✕</button>
  </div>
</template>
