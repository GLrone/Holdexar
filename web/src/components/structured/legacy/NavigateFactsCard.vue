<script setup lang="ts">
import { computed } from 'vue'
import type { PilotNavigateFacts } from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'

const props = defineProps<{
  data: PilotNavigateFacts
}>()

const { t } = useI18n()
const c = computed(() => props.data)

function navModuleName(target: string): string {
  return t(`pilot.nav.${target}` as MessageKey)
}
</script>

<template>
  <div class="hsui-navigate-card">
    <div class="pcard-receipt">
      <div class="pcard-receipt__dot"></div>
      <span class="pcard-receipt__text">{{ t('pilot.nav.done', { module: navModuleName(c.target) }) }}</span>
    </div>
  </div>
</template>

<style scoped>
.hsui-navigate-card {
  width: 100%;
}

.pcard-receipt {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.15));
  border-radius: var(--radius-sm, 6px);
  font-size: 12px;
}

.pcard-receipt__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent, #66c0f4);
}

.pcard-receipt__text {
  color: var(--text-secondary, #c7d5e0);
}
</style>
