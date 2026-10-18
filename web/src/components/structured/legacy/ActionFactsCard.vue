<script setup lang="ts">
import { computed } from 'vue'
import type { PilotActionFacts } from '@/api/client'
import { useI18n } from '@/locales'
import { pilotCoverUrl } from '@/lib/pilotView'
import HlImg from '@/components/ui/HlImg.vue'
import { fen } from '../helpers'

const props = defineProps<{
  data: PilotActionFacts
}>()

const { t } = useI18n()
const c = computed(() => props.data)

function actionReceipt(f: PilotActionFacts): string {
  if (f.action === 'monitor_add') return t('pilot.action.monitor', { name: f.name ?? '—' })
  if (f.targetType === 'historic_low') return t('pilot.action.alertLow', { name: f.name ?? '—' })
  return t('pilot.action.alertPrice', { name: f.name ?? '—', price: fen(f.targetValueFen) })
}
</script>

<template>
  <div class="hsui-action-card">
    <div class="pcard-receipt">
      <div class="pcard-receipt__dot"></div>
      <router-link
        v-if="c.appid"
        :to="`/game/${c.appid}`"
        class="pcard-receipt__cover-link"
        :title="c.name || `AppID ${c.appid}`"
      >
        <HlImg
          :src="pilotCoverUrl(c.appid)"
          :alt="c.name || `AppID ${c.appid}`"
          loading="lazy"
          class="pcard-receipt__cover"
        >
          <template #fallback>
            <span class="pcard-receipt__cover-fallback">{{ (c.name || `AppID ${c.appid}`).slice(0, 2) }}</span>
          </template>
        </HlImg>
      </router-link>
      <span class="pcard-receipt__text">{{ actionReceipt(c) }}</span>
      <router-link v-if="c.action === 'monitor_add'" class="pcard-receipt__link" to="/pool">
        {{ t('pilot.action.toFollows') }}
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.hsui-action-card {
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
  background: var(--success, #a4d007);
}

.pcard-receipt__cover-link {
  display: inline-flex;
  flex-shrink: 0;
}

.pcard-receipt__cover {
  width: 32px;
  height: 18px;
  border-radius: 2px;
  object-fit: cover;
}

.pcard-receipt__text {
  color: var(--text-secondary, #c7d5e0);
  flex: 1;
}

.pcard-receipt__link {
  color: var(--accent, #66c0f4);
  font-size: 11px;
  text-decoration: none;
}

.pcard-receipt__link:hover {
  text-decoration: underline;
}
</style>
