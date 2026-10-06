<script setup lang="ts">
import { computed } from 'vue'

import type { PilotRegionsFacts } from '@/api/client'
import { formatCnyFen } from '@/api/regions'
import { useI18n } from '@/locales'
import RegionFlag from '@/components/RegionFlag.vue'

const props = defineProps<{ card: PilotRegionsFacts }>()
const { t } = useI18n()

const maxFen = computed(() => Math.max(...props.card.items.map((r) => r.cnyFen), 1))

function isMine(code: string): boolean {
  return !!props.card.accountRegion && code.toLowerCase() === props.card.accountRegion
}

function barWidth(fen: number): string {
  return `${Math.max(12, Math.round((fen / maxFen.value) * 100))}%`
}

const cheapest = computed(() => props.card.items[0])
const mine = computed(() => props.card.items.find((r) => isMine(r.region)))

const verdict = computed(() => {
  const m = mine.value
  const c = cheapest.value
  if (!m || !c) return ''
  if (c.cnyFen >= m.cnyFen) return t('pilot.regions.mineCheapest')
  const save = m.cnyFen - c.cnyFen
  return t('pilot.regions.save', {
    amount: formatCnyFen(save),
    pct: Math.round((save / m.cnyFen) * 100),
  })
})
</script>

<template>
  <div class="preg">
    <div class="preg__header">
      <div class="preg__title" :title="card.name || `AppID ${card.appid}`">
        <svg class="preg__title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="10" />
          <line x1="2" y1="12" x2="22" y2="12" />
          <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
        </svg>
        <span>{{ t('pilot.regions.title', { name: card.name || `AppID ${card.appid}` }) }}</span>
      </div>
    </div>

    <!-- 智能省钱洞察横幅置顶 -->
    <div v-if="verdict" class="preg__verdict">
      <svg class="preg__verdict-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="10" />
        <polyline points="12 6 12 12 16 14" />
      </svg>
      <span>{{ verdict }}</span>
    </div>

    <div class="preg__list">
      <div
        v-for="(r, i) in card.items"
        :key="r.region"
        class="preg__item"
        :class="{ 'is-mine': isMine(r.region), 'is-best': i === 0 }"
      >
        <div class="preg__item-left">
          <RegionFlag :code="r.region.toLowerCase()" compact class="preg__region" />
          <div class="preg__bar-track">
            <i class="preg__bar-fill" :style="{ width: barWidth(r.cnyFen) }"></i>
          </div>
        </div>

        <div class="preg__item-right">
          <span class="preg__price">{{ formatCnyFen(r.cnyFen) }}</span>
          <span v-if="r.discount" class="preg__disc">-{{ r.discount }}%</span>
          <span v-if="i === 0" class="preg__tag preg__tag--best">
            <svg class="preg__crown" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M2 4l3 12h14l3-12-5 6-5-8-5 8z" />
            </svg>
            {{ t('pilot.regions.cheapest') }}
          </span>
          <span v-else-if="isMine(r.region)" class="preg__tag preg__tag--mine">{{ t('pilot.regions.mine') }}</span>
        </div>
      </div>
    </div>

    <div v-if="card.count > card.items.length" class="preg__foot">
      {{ t('pilot.regions.more', { count: card.count }) }}
    </div>
  </div>
</template>

<style scoped>
.preg {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 100%;
  min-width: 0;
}

.preg__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.preg__title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preg__title-icon {
  width: 15px;
  height: 15px;
  color: var(--accent);
  flex-shrink: 0;
}

.preg__verdict {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  background: var(--success-a15);
  color: var(--success);
  border: 1px solid var(--success-a30);
  font-size: 12px;
  font-weight: 600;
}

.preg__verdict-icon {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

.preg__list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.preg__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
  min-width: 0;
  transition: all var(--transition);
}

.preg__item:hover {
  border-color: var(--border-strong);
  background: var(--surface-panel);
}

.preg__item.is-best {
  background: var(--accent-a08);
  border-color: var(--accent-a30);
}

.preg__item.is-mine {
  border-left: 3px solid var(--accent);
}

.preg__item-left {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}

.preg__region {
  flex-shrink: 0;
  width: 60px;
}

.preg__bar-track {
  flex: 1;
  min-width: 40px;
  height: 6px;
  border-radius: 999px;
  background: var(--surface-track);
  overflow: hidden;
}

.preg__bar-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--text-faint);
  transition: width 0.3s ease;
}

.preg__item.is-best .preg__bar-fill {
  background: var(--success);
}

.preg__item.is-mine .preg__bar-fill {
  background: var(--accent);
}

.preg__item-right {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.preg__price {
  font-weight: 700;
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-mono);
}

.preg__disc {
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  background: var(--success);
  color: var(--ink-on-fill);
  font-size: 10px;
  font-weight: 700;
}

.preg__tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.3;
}

.preg__crown {
  width: 10px;
  height: 10px;
}

.preg__tag--best {
  background: var(--success);
  color: var(--ink-on-fill);
}

.preg__tag--mine {
  background: var(--accent-a20);
  color: var(--accent);
}

.preg__foot {
  font-size: 11px;
  color: var(--text-muted);
  text-align: right;
  padding-top: 2px;
}
</style>
