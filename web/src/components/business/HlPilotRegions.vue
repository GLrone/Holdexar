<script setup lang="ts">
/**
 * 领航台地区对比卡：各地区折合人民币的现价横条（便宜的在上），
 * 标出最低价地区与账号结算区，并给出换区能省多少。样式只取设计 token。
 */
import { computed } from 'vue'

import type { PilotRegionsFacts } from '@/api/client'
import { formatCnyFen } from '@/api/regions'
import { useI18n } from '@/locales'
import RegionFlag from '@/components/RegionFlag.vue'

const props = defineProps<{ card: PilotRegionsFacts }>()
const { t } = useI18n()

const maxFen = computed(() => Math.max(...props.card.items.map((r) => r.cnyFen)))

function isMine(code: string): boolean {
  return !!props.card.accountRegion && code.toLowerCase() === props.card.accountRegion
}

function barWidth(fen: number): string {
  return `${Math.max(8, Math.round((fen / maxFen.value) * 100))}%`
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
    <div class="preg__title" :title="card.name || `AppID ${card.appid}`">
      {{ t('pilot.regions.title', { name: card.name || `AppID ${card.appid}` }) }}
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
          <div class="preg__bar">
            <i :style="{ width: barWidth(r.cnyFen) }"></i>
          </div>
        </div>
        <div class="preg__item-right">
          <span class="preg__price">{{ formatCnyFen(r.cnyFen) }}</span>
          <span v-if="r.discount" class="preg__disc">-{{ r.discount }}%</span>
          <span v-if="i === 0" class="preg__tag preg__tag--best">{{ t('pilot.regions.cheapest') }}</span>
          <span v-else-if="isMine(r.region)" class="preg__tag">{{ t('pilot.regions.mine') }}</span>
        </div>
      </div>
    </div>
    <div v-if="verdict" class="preg__verdict">{{ verdict }}</div>
    <div v-if="card.count > card.items.length" class="preg__foot">
      {{ t('pilot.regions.more', { count: card.count }) }}
    </div>
  </div>
</template>

<style scoped>
.preg {
  display: grid;
  gap: 8px;
  max-width: 100%;
  min-width: 0;
  overflow: hidden;
}

.preg__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preg__list {
  display: grid;
  gap: 5px;
  min-width: 0;
}

.preg__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  background: var(--surface-inset);
  min-width: 0;
  font-size: 12px;
  transition: background 0.15s ease;
}

.preg__item.is-best {
  background: var(--accent-a10);
}

.preg__item-left {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.preg__region {
  flex-shrink: 0;
  width: 58px;
  font-size: 11px;
}

.preg__bar {
  flex: 1;
  min-width: 24px;
  height: 5px;
  border-radius: 3px;
  background: var(--border-soft);
  overflow: hidden;
}

.preg__bar i {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--text-secondary);
  opacity: 0.45;
}

.preg__item.is-best .preg__bar i {
  background: var(--accent);
  opacity: 1;
}

.preg__item.is-mine .preg__bar i {
  background: var(--accent);
  opacity: 0.8;
}

.preg__item-right {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.preg__price {
  font-weight: 600;
  color: var(--text-primary);
  font-size: 11px;
}

.preg__disc {
  font-size: 10px;
  font-weight: 700;
  color: var(--success);
}

.preg__tag {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 10px;
}

.preg__tag--best {
  background: var(--accent);
  color: var(--ink-on-fill);
  font-weight: 600;
}

.preg__verdict {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  padding: 4px 8px;
  border-radius: 6px;
  background: var(--surface-inset);
}

.preg__foot {
  font-size: 11px;
  color: var(--text-secondary);
}
</style>
