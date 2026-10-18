<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PilotGameFacts, PilotGameFactsItem } from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import { pilotCoverUrl, positivePct, ratingClass } from '@/lib/pilotView'
import HlImg from '@/components/ui/HlImg.vue'
import { dateOf, fen, ratingText } from '../helpers'

const props = defineProps<{
  data: PilotGameFacts
}>()

const { t } = useI18n()
const c = computed(() => props.data)

const activeFilter = ref('all')

interface FilterChipDef {
  key: string
  labelKey: MessageKey
  count: number
}

function hasSimplifiedChinese(support: string | null | undefined): boolean {
  if (!support) return false
  return /(?:schinese|simplified|[\u7b80])/i.test(support)
}

const filterChips = computed<FilterChipDef[]>(() => {
  const items = c.value.items || []
  if (items.length < 2) return []

  const chips: FilterChipDef[] = [
    { key: 'all', labelKey: 'pilot.filter.all', count: items.length },
  ]

  const rating90Count = items.filter((g) => {
    const p = positivePct(g.positiveRate)
    return p !== null && p >= 90
  }).length
  if (rating90Count > 0) {
    chips.push({ key: 'rating90', labelKey: 'pilot.filter.rating90', count: rating90Count })
  }

  const discCount = items.filter((g) => (g.discount || 0) > 0).length
  if (discCount > 0) {
    chips.push({ key: 'discounted', labelKey: 'pilot.filter.discounted', count: discCount })
  }

  const budget50Count = items.filter((g) => typeof g.cnyFen === 'number' && g.cnyFen > 0 && g.cnyFen <= 5000).length
  if (budget50Count > 0) {
    chips.push({ key: 'budget50', labelKey: 'pilot.filter.budget50', count: budget50Count })
  }

  const cnCount = items.filter((g) => hasSimplifiedChinese(g.chineseSupport)).length
  if (cnCount > 0) {
    chips.push({ key: 'chinese', labelKey: 'pilot.filter.chinese', count: cnCount })
  }

  return chips
})

const filteredItems = computed<PilotGameFactsItem[]>(() => {
  const items = c.value.items || []
  const filterKey = activeFilter.value
  if (filterKey === 'all') return items

  if (filterKey === 'rating90') {
    return items.filter((g) => {
      const p = positivePct(g.positiveRate)
      return p !== null && p >= 90
    })
  }
  if (filterKey === 'discounted') {
    return items.filter((g) => (g.discount || 0) > 0)
  }
  if (filterKey === 'budget50') {
    return items.filter((g) => typeof g.cnyFen === 'number' && g.cnyFen > 0 && g.cnyFen <= 5000)
  }
  if (filterKey === 'chinese') {
    return items.filter((g) => hasSimplifiedChinese(g.chineseSupport))
  }
  return items
})

function noteText(n: NonNullable<PilotGameFactsItem['note']>): string {
  const parts: string[] = []
  const main = t(`pilot.row.${n.key}` as MessageKey)
  if (main) parts.push(main)
  if (n.v) parts.push(n.v)
  const d = dateOf(n.at ?? null)
  if (d) parts.push(d)
  return parts.join(' · ')
}

function noteTone(key: string): string | null {
  return key === 'ev_new_historical_low' || key === 'ev_price_drop' ? 'is-ok' : null
}
</script>

<template>
  <div class="hsui-games-card">
    <div class="pcard-header-bar">
      <div class="pcard-header-bar__title">
        <svg class="pcard-header-bar__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="2" y="3" width="20" height="14" rx="2" />
          <line x1="8" y1="21" x2="16" y2="21" />
          <line x1="12" y1="17" x2="12" y2="21" />
        </svg>
        <span>{{ c.titleKey ? t(`pilot.rows.${c.titleKey}` as MessageKey) : t('pilot.facts.gamesTitle') }}</span>
      </div>
      <span v-if="c.total != null && c.total > c.items.length" class="pcard-header-bar__badge">
        {{ t('pilot.facts.more', { n: c.total }) }}
      </span>
    </div>

    <div v-if="filterChips.length > 1" class="pcard-filter-pills">
      <button
        v-for="chip in filterChips"
        :key="chip.key"
        type="button"
        class="pcard-filter-pill"
        :class="{ 'is-active': activeFilter === chip.key }"
        @click="activeFilter = chip.key"
      >
        <span>{{ t(chip.labelKey) }}</span>
        <span class="pcard-filter-pill__count">{{ chip.count }}</span>
      </button>
    </div>

    <div v-if="filteredItems.length === 0" class="pcard-empty-state">
      <span>{{ t('pilot.filter.empty') }}</span>
      <button type="button" class="pcard-empty-state__reset" @click="activeFilter = 'all'">
        {{ t('pilot.filter.reset') }}
      </button>
    </div>

    <div v-else class="pcard-game-list">
      <router-link
        v-for="(g, gi) in filteredItems"
        :key="g.appid"
        class="pcard-game-item"
        :to="`/game/${g.appid}`"
      >
        <span class="pcard-game-item__index">{{ gi + 1 }}</span>
        <HlImg :src="pilotCoverUrl(g.appid)" :alt="g.name || `AppID ${g.appid}`" loading="lazy" class="pcard-game-item__cover">
          <template #fallback>
            <span class="pcard-game-item__fallback">{{ (g.name || `AppID ${g.appid}`).slice(0, 2) }}</span>
          </template>
        </HlImg>

        <div class="pcard-game-item__content">
          <div class="pcard-game-item__top">
            <span class="pcard-game-item__title" :title="g.name || `AppID ${g.appid}`">{{ g.name || `AppID ${g.appid}` }}</span>
            <span v-if="hasSimplifiedChinese(g.chineseSupport)" class="pcard-cn-tag">
              {{ t('pilot.filter.chinese') }}
            </span>
          </div>

          <div class="pcard-game-item__bottom">
            <span v-if="ratingText(g.positiveRate)" class="pcard-game-item__rating" :class="ratingClass(g.positiveRate)">
              {{ ratingText(g.positiveRate) }}
            </span>
            <span class="pcard-game-item__price-block">
              <span class="pcard-game-item__price">{{ fen(g.cnyFen) }}</span>
              <span v-if="g.discount" class="pcard-disc-tag pcard-disc-tag--xs">-{{ g.discount }}%</span>
            </span>
            <span v-if="g.note" class="pcard-game-item__note" :class="noteTone(g.note.key)">
              {{ noteText(g.note) }}
            </span>
          </div>
        </div>

        <svg class="pcard-game-item__arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.hsui-games-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
}

.pcard-header-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
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

.pcard-header-bar__badge {
  font-size: 11px;
  color: var(--text-muted, #8f98a0);
}

.pcard-filter-pills {
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
  padding-bottom: 2px;
}

.pcard-filter-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 11px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.2));
  color: var(--text-muted, #8f98a0);
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.1));
  cursor: pointer;
  white-space: nowrap;
}

.pcard-filter-pill.is-active {
  background: var(--accent-a15, rgba(102, 192, 244, 0.15));
  color: var(--accent, #66c0f4);
  border-color: var(--accent, #66c0f4);
}

.pcard-filter-pill__count {
  font-size: 10px;
  opacity: 0.8;
}

.pcard-empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 20px 0;
  font-size: 12px;
  color: var(--text-muted, #8f98a0);
}

.pcard-empty-state__reset {
  color: var(--accent, #66c0f4);
  background: transparent;
  border: none;
  cursor: pointer;
  text-decoration: underline;
  padding: 0;
}

.pcard-game-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pcard-game-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  background: var(--surface-inset, rgba(0, 0, 0, 0.2));
  border: 1px solid var(--border-soft, rgba(102, 192, 244, 0.08));
  border-radius: var(--radius-sm, 6px);
  text-decoration: none;
  transition: background var(--transition, 0.22s ease), border-color var(--transition, 0.22s ease);
}

.pcard-game-item:hover {
  background: var(--hover-soft, rgba(102, 192, 244, 0.12));
  border-color: var(--border-strong, rgba(102, 192, 244, 0.28));
}

.pcard-game-item__index {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-dim, #5a7080);
  width: 14px;
  text-align: center;
  flex-shrink: 0;
}

.pcard-game-item__cover {
  width: 58px;
  height: 32px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
  background: var(--surface-card-2, #2a475e);
}

.pcard-game-item__fallback {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: var(--text-dim, #5a7080);
}

.pcard-game-item__content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pcard-game-item__top {
  display: flex;
  align-items: center;
  gap: 6px;
}

.pcard-game-item__title {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary, #d9e3ec);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pcard-cn-tag {
  font-size: 9px;
  padding: 1px 4px;
  background: var(--accent-a15);
  color: var(--accent, #66c0f4);
  border-radius: 2px;
  flex-shrink: 0;
}

.pcard-game-item__bottom {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pcard-game-item__rating {
  font-size: 10px;
  font-weight: 600;
}

.pcard-game-item__rating.is-rate-good {
  color: var(--rate-good, #2ecc71);
}

.pcard-game-item__rating.is-rate-mid {
  color: var(--rate-mid, #f1c40f);
}

.pcard-game-item__rating.is-rate-low {
  color: var(--rate-low, #e74c3c);
}

.pcard-game-item__price-block {
  display: flex;
  align-items: center;
  gap: 4px;
}

.pcard-game-item__price {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary, #c7d5e0);
}

.pcard-disc-tag--xs {
  font-size: 9px;
  padding: 1px 3px;
  background: var(--success, #a4d007);
  color: var(--ink-on-fill);
  font-weight: 700;
  border-radius: 2px;
}

.pcard-game-item__note {
  font-size: 10px;
  color: var(--text-dim, #5a7080);
}

.pcard-game-item__note.is-ok {
  color: var(--success, #a4d007);
}

.pcard-game-item__arrow {
  width: 12px;
  height: 12px;
  color: var(--text-dim, #5a7080);
  flex-shrink: 0;
}
</style>
