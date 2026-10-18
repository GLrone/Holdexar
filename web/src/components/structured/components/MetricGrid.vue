<script setup lang="ts">
import { computed } from 'vue'
import type { MetricGridData, MetricItem, StructuredDensity } from '../types'
import HlTooltip from '@/components/ui/HlTooltip.vue'

const props = withDefaults(
  defineProps<{
    data: MetricGridData
    density?: StructuredDensity
  }>(),
  { density: 'standard' },
)

const emit = defineEmits<{
  (e: 'metric-click', item: MetricItem): void
}>()

const items = computed(() => props.data?.items ?? [])
const isCompact = computed(() => props.density === 'compact')

function onItemClick(item: MetricItem) {
  if (item.clickable) {
    emit('metric-click', item)
  }
}
</script>

<template>
  <div
    class="hsui-metric-grid"
    :class="[
      `is-density-${density}`,
      { 'is-single': items.length === 1 }
    ]"
  >
    <div
      v-for="item in items"
      :key="item.id"
      class="hsui-metric-card"
      :class="[
        `is-${item.status || 'normal'}`,
        { 'is-clickable': item.clickable }
      ]"
      @click="onItemClick(item)"
    >
      <div class="hsui-metric-card__header">
        <span class="hsui-metric-card__label" :title="item.label">{{ item.label }}</span>
        <HlTooltip v-if="item.tooltip" :content="item.tooltip">
          <span class="hsui-metric-card__info-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-3.5 h-3.5">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="16" x2="12" y2="12" />
              <line x1="12" y1="8" x2="12.01" y2="8" />
            </svg>
          </span>
        </HlTooltip>
      </div>

      <div class="hsui-metric-card__value-row">
        <span class="hsui-metric-card__val hl-num">{{ item.value }}</span>
        <span v-if="item.unit" class="hsui-metric-card__unit">{{ item.unit }}</span>
      </div>

      <div v-if="item.diff || item.benchmark || item.subText" class="hsui-metric-card__footer">
        <span
          v-if="item.diff"
          class="hsui-metric-card__diff"
          :class="`is-${item.diffTone || 'neutral'}`"
        >
          <svg
            v-if="item.diffDirection === 'up'"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2.5"
            stroke-linecap="round"
            stroke-linejoin="round"
            class="hsui-metric-card__arrow"
          >
            <path d="M18 15l-6-6-6 6" />
          </svg>
          <svg
            v-else-if="item.diffDirection === 'down'"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2.5"
            stroke-linecap="round"
            stroke-linejoin="round"
            class="hsui-metric-card__arrow"
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
          {{ item.diff }}
        </span>

        <span v-if="item.benchmark" class="hsui-metric-card__bench">
          {{ item.benchmark }}
        </span>

        <span v-else-if="item.subText" class="hsui-metric-card__sub">
          {{ item.subText }}
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hsui-metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
  width: 100%;
}

.hsui-metric-grid.is-single {
  grid-template-columns: 1fr;
}

.hsui-metric-grid.is-density-compact {
  gap: 6px;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
}

.hsui-metric-card {
  display: flex;
  flex-direction: column;
  padding: 12px 14px;
  background: var(--surface-card, #1b2838);
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.14));
  border-radius: var(--radius, 10px);
  transition: border-color var(--transition, 0.22s ease), transform var(--transition, 0.22s ease);
}

.hsui-metric-grid.is-density-compact .hsui-metric-card {
  padding: 8px 10px;
}

.hsui-metric-card.is-clickable {
  cursor: pointer;
}

.hsui-metric-card.is-clickable:hover {
  border-color: var(--accent, #66c0f4);
  transform: translateY(-1px);
}

.hsui-metric-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  margin-bottom: 6px;
}

.hsui-metric-card__label {
  font-size: 12px;
  color: var(--text-muted, #8f98a0);
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.hsui-metric-card__info-icon {
  display: inline-flex;
  align-items: center;
  color: var(--text-dim, #5a7080);
  cursor: help;
}

.hsui-metric-card__info-icon:hover {
  color: var(--text-secondary, #c7d5e0);
}

.hsui-metric-card__value-row {
  display: flex;
  align-items: baseline;
  gap: 4px;
  margin-bottom: 4px;
}

.hsui-metric-card__val {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary, #d9e3ec);
  line-height: 1.15;
  letter-spacing: -0.02em;
}

.hsui-metric-grid.is-density-compact .hsui-metric-card__val {
  font-size: 17px;
}

.hsui-metric-card.is-good .hsui-metric-card__val {
  color: var(--success, #a4d007);
}

.hsui-metric-card.is-warn .hsui-metric-card__val {
  color: var(--warning, #f59e0b);
}

.hsui-metric-card.is-bad .hsui-metric-card__val {
  color: var(--danger, #e74c3c);
}

.hsui-metric-card__unit {
  font-size: 12px;
  color: var(--text-muted, #8f98a0);
  font-weight: 400;
}

.hsui-metric-card__footer {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 11px;
  line-height: 1.3;
}

.hsui-metric-card__diff {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-weight: 600;
}

.hsui-metric-card__diff.is-good {
  color: var(--success, #a4d007);
}

.hsui-metric-card__diff.is-warn {
  color: var(--warning, #f59e0b);
}

.hsui-metric-card__diff.is-bad {
  color: var(--danger, #e74c3c);
}

.hsui-metric-card__diff.is-neutral {
  color: var(--text-muted, #8f98a0);
}

.hsui-metric-card__arrow {
  width: 12px;
  height: 12px;
}

.hsui-metric-card__bench,
.hsui-metric-card__sub {
  color: var(--text-dim, #5a7080);
}
</style>
