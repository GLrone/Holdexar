<script setup lang="ts">
import { HlIcon } from './index'
import { useI18n } from '@/locales'

const model = defineModel<string>({ default: '' })

defineProps<{ placeholder?: string; disabled?: boolean }>()

const { t } = useI18n()
</script>

<template>
  <span class="hl-date-editor">
    <input
      v-model="model"
      :placeholder="placeholder ?? t('common.selectDate')"
      :disabled="disabled"
      style="
        border: none;
        outline: none;
        background: transparent;
        font: inherit;
        font-size: 13px;
        color: var(--text-primary);
        width: 100%;
      "
    />
    <HlIcon name="calendar" />
  </span>
</template>
