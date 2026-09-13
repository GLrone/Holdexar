<script setup lang="ts">
import { useI18n } from '@/locales'

const { t } = useI18n()
</script>

<template>
  <span class="hl-spinner" role="status" :aria-label="t('common.loading')" />
</template>
