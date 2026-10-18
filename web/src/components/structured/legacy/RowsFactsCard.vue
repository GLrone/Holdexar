<script setup lang="ts">
import { computed } from 'vue'
import type { PilotRowsFacts } from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import { pilotCoverUrl } from '@/lib/pilotView'
import CurrencyFlag from '@/components/CurrencyFlag.vue'
import HlImg from '@/components/ui/HlImg.vue'
import { dateOf, fen } from '../helpers'

const props = defineProps<{
  data: PilotRowsFacts
}>()

const { t } = useI18n()
const c = computed(() => props.data)

type RowsRow = PilotRowsFacts['rows'][number]

function rowLabel(r: RowsRow): string {
  return r.kindKey ? t(r.kindKey as MessageKey) : r.k
}

function rowValue(r: RowsRow): string {
  if (!r.vKey) return ''
  const data: Record<string, unknown> = { ...(r.data ?? {}) }
  for (const [key, val] of Object.entries(data)) {
    if (key.endsWith('Fen') && typeof val === 'number' && val > 0) {
      data[key.slice(0, -3)] = fen(val)
    }
  }
  return t(`pilot.row.${r.vKey}` as MessageKey, data)
}
</script>

<template>
  <div class="hsui-rows-card">
    <div class="pcard-header-bar" :class="{ 'has-cover': Boolean(c.appid) }">
      <router-link
        v-if="c.appid"
        :to="`/game/${c.appid}`"
        class="pcard-header-bar__cover-link"
        :title="c.name || `AppID ${c.appid}`"
      >
        <HlImg
          :src="pilotCoverUrl(c.appid)"
          :alt="c.name || `AppID ${c.appid}`"
          loading="lazy"
          class="pcard-header-bar__cover"
        >
          <template #fallback>
            <span class="pcard-header-bar__cover-fallback">{{ (c.name || `AppID ${c.appid}`).slice(0, 2) }}</span>
          </template>
        </HlImg>
      </router-link>
      <div class="pcard-header-bar__title">
        <svg v-if="!c.appid" class="pcard-header-bar__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="16" x2="12" y2="12" />
          <line x1="12" y1="8" x2="12.01" y2="8" />
        </svg>
        <span>{{ t(`pilot.rows.${c.titleKey}` as MessageKey) }}</span>
      </div>
      <span v-if="c.name" class="pcard-header-bar__subtitle">{{ c.name }}</span>
    </div>

    <div class="pcard-diagnostics-grid">
      <div v-for="(r, ri) in c.rows" :key="ri" class="pcard-diag-row">
        <div class="pcard-diag-row__head">
          <span class="pcard-diag-row__label">
            <template v-if="c.titleKey === 'rates'">
              <CurrencyFlag :code="r.k" />
              <span class="pcard-code-mono">({{ r.k }})</span>
            </template>
            <template v-else>
              {{ rowLabel(r) }}
            </template>
          </span>
          <span v-if="r.at" class="pcard-diag-row__time">{{ dateOf(r.at) }}</span>
        </div>

        <div v-if="rowValue(r) || r.v" class="pcard-diag-row__status">
          <span class="pcard-status-pill" :class="r.tone ? `is-${r.tone}` : 'is-default'">
            <i v-if="r.tone" class="pcard-status-pill__dot" aria-hidden="true"></i>
            <template v-if="rowValue(r)">{{ rowValue(r) }}</template>
            <template v-if="rowValue(r) && r.v"> · </template>
            <template v-if="r.v">{{ r.v }}</template>
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hsui-rows-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
}

.pcard-header-bar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pcard-header-bar__cover {
  width: 36px;
  height: 20px;
  border-radius: 2px;
  object-fit: cover;
}

.pcard-header-bar__cover-fallback {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  color: var(--text-dim, #5a7080);
}

.pcard-header-bar__title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, #d9e3ec);
}

.pcard-header-bar__icon {
  width: 15px;
  height: 15px;
  color: var(--accent, #66c0f4);
}

.pcard-header-bar__subtitle {
  font-size: 11px;
  color: var(--text-muted, #8f98a0);
}

.pcard-diagnostics-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard-diag-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 8px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.2));
  border-radius: var(--radius-sm, 6px);
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.08));
}

.pcard-diag-row__head {
  display: flex;
  align-items: center;
  gap: 6px;
}

.pcard-diag-row__label {
  font-size: 12px;
  color: var(--text-secondary, #c7d5e0);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.pcard-code-mono {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}

.pcard-diag-row__time {
  font-size: 10px;
  color: var(--text-dim, #5a7080);
}

.pcard-status-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border-radius: var(--radius-sm, 6px);
  font-size: 11px;
  font-weight: 500;
  background: var(--surface-card, #1b2838);
  color: var(--text-secondary, #c7d5e0);
}

.pcard-status-pill.is-ok {
  color: var(--success, #a4d007);
  background: var(--success-a15, rgba(164, 208, 7, 0.15));
}

.pcard-status-pill.is-warn {
  color: var(--warning, #f59e0b);
  background: var(--warning-a40, rgba(245, 158, 11, 0.2));
}

.pcard-status-pill.is-bad {
  color: var(--danger, #e74c3c);
  background: var(--danger-a15, rgba(231, 76, 60, 0.15));
}

.pcard-status-pill__dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}
</style>
