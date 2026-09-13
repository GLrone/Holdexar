<script setup lang="ts">
import { useI18n } from '@/locales'

const { t } = useI18n()

function backTop() {
  window.scrollTo({ top: 0, behavior: 'smooth' })
}
</script>

<template>
  <button type="button" class="hl-backtop" :title="t('shell.backtop')" @click="backTop">↑</button>
</template>
