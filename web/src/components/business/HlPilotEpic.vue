<script setup lang="ts">
import { computed } from 'vue'

import type { PilotEpicFacts, PilotEpicOfferItem } from '@/api/client'
import { useI18n } from '@/locales'
import HlImg from '@/components/ui/HlImg.vue'

const props = defineProps<{ card: PilotEpicFacts }>()
const { t } = useI18n()

function daysUntil(day: string): number {
  const [y, m, d] = (day || '').split('-').map(Number)
  if (!y || !m || !d) return 0
  const now = new Date()
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  return Math.round((Date.UTC(y, m - 1, d) - today) / 86_400_000)
}

function statusText(o: PilotEpicOfferItem): string {
  if (o.upcoming) {
    const n = daysUntil(o.start)
    if (n <= 0) return t('epicFree.startsToday')
    if (n === 1) return t('epicFree.startsTomorrow')
    return t('epicFree.startsIn', { n })
  }
  const n = daysUntil(o.end)
  if (n <= 0) return t('epicFree.endsToday')
  return t('epicFree.endsIn', { n })
}

const mobileEndText = computed(() => {
  const end = props.card.mobile?.end
  if (!end) return ''
  const n = daysUntil(end)
  if (n <= 0) return t('epicFree.endsToday')
  return t('epicFree.endsIn', { n })
})

const sortedOffers = computed(() => {
  const offers = props.card.offers || []
  const live = offers
    .filter((o) => !o.upcoming)
    .sort((a, b) => daysUntil(a.end) - daysUntil(b.end))
  const upcoming = offers
    .filter((o) => o.upcoming)
    .sort((a, b) => daysUntil(a.start) - daysUntil(b.start))
  return [...live, ...upcoming]
})
</script>

<template>
  <div class="pepic">
    <div class="pepic-header">
      <div class="pepic-header__left">
        <img src="/assets/logo_epic.ico" alt="" class="pepic-header__logo" />
        <span class="pepic-header__title">{{ t('epicFree.title') }}</span>
      </div>
      <div class="pepic-header__right">
        <span v-if="card.fetchedAt" class="pepic-header__time">
          {{ t('epicFree.updatedAt', { time: card.fetchedAt.slice(11, 16) }) }}
        </span>
        <a
          class="pepic-header__hub"
          href="https://store.epicgames.com/en-US/free-games"
          target="_blank"
          rel="noopener noreferrer"
        >
          {{ t('epicFree.goHub') }} →
        </a>
      </div>
    </div>

    <div v-if="sortedOffers.length === 0 && !card.mobile" class="pepic-empty">
      {{ t('epicFree.empty') }}
    </div>

    <div v-else class="pepic-grid">
      <a
        v-for="o in sortedOffers"
        :key="`${o.url}-${o.start}`"
        class="pepic-card"
        :class="{ 'pepic-card--upcoming': o.upcoming }"
        :href="o.url"
        target="_blank"
        rel="noopener noreferrer"
      >
        <div class="pepic-card__media">
          <HlImg
            class="pepic-card__img"
            :src="o.image ?? undefined"
            :alt="o.titleCn || o.title"
            loading="lazy"
          >
            <template #fallback>
              <span class="pepic-card__fallback">{{ (o.titleCn || o.title || 'EPIC').slice(0, 2) }}</span>
            </template>
          </HlImg>

          <span class="pepic-card__free" :class="{ 'pepic-card__free--ghost': o.upcoming }">
            {{ t('epicFree.free') }}
          </span>

          <span
            class="pepic-card__status"
            :class="{ 'pepic-card__status--upcoming': o.upcoming }"
          >
            <span class="pepic-card__state">
              {{ o.upcoming ? t('epicFree.upcoming') : t('epicFree.live') }}
            </span>
            <span class="pepic-card__countdown">{{ statusText(o) }}</span>
          </span>

          <img src="/assets/logo_epic.ico" alt="" class="pepic-card__mark" />
        </div>

        <div class="pepic-card__body">
          <div class="pepic-card__title-row">
            <span class="pepic-card__name" :title="o.titleCn || o.title">
              {{ o.titleCn || o.title }}
            </span>
          </div>

          <div class="pepic-card__footer">
            <span v-if="o.upcoming" class="pepic-card__start">
              {{ t('epicFree.startsOn', { date: o.start }) }}
            </span>
            <div v-else class="pepic-card__action">
              <span v-if="o.priceOriginal" class="pepic-card__orig">{{ o.priceOriginal }}</span>
              <span class="pepic-card__claim">{{ t('epicFree.claim') }} ↗</span>
            </div>
          </div>
        </div>
      </a>

      <!-- 移动端白送 -->
      <a
        v-if="card.mobile"
        class="pepic-card pepic-card--mobile"
        :href="card.mobile.url"
        target="_blank"
        rel="noopener noreferrer"
      >
        <div class="pepic-card__media">
          <HlImg
            class="pepic-card__img"
            :src="card.mobile.image ?? undefined"
            :alt="card.mobile.title || t('epicFree.mobileCard')"
            loading="lazy"
          >
            <template #fallback>
              <span class="pepic-card__fallback">{{ t('epicFree.mobile').slice(0, 2) }}</span>
            </template>
          </HlImg>

          <span class="pepic-card__free">{{ t('epicFree.free') }}</span>

          <span class="pepic-card__status">
            <span class="pepic-card__state pepic-card__state--mobile">
              {{ t('epicFree.mobile') }}
            </span>
            <span v-if="mobileEndText" class="pepic-card__countdown">{{ mobileEndText }}</span>
          </span>

          <img src="/assets/logo_epic.ico" alt="" class="pepic-card__mark" />
        </div>

        <div class="pepic-card__body">
          <div class="pepic-card__title-row">
            <span class="pepic-card__name" :title="card.mobile.title || t('epicFree.mobileCard')">
              {{ card.mobile.title || t('epicFree.mobileCard') }}
            </span>
          </div>

          <div class="pepic-card__footer">
            <div class="pepic-card__action">
              <span v-if="card.mobile.worth" class="pepic-card__orig">{{ card.mobile.worth }}</span>
              <span class="pepic-card__claim">{{ card.mobile.direct === false ? t('epicFree.mobileInApp') : t('epicFree.claim') }} ↗</span>
            </div>
          </div>
        </div>
      </a>
    </div>
  </div>
</template>

<style scoped>
.pepic {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 100%;
}

.pepic-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.pepic-header__left {
  display: flex;
  align-items: center;
  gap: 7px;
}

.pepic-header__logo {
  width: 16px;
  height: 16px;
  display: block;
}

.pepic-header__title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.pepic-header__right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.pepic-header__time {
  font-size: 11px;
  color: var(--text-muted);
}

.pepic-header__hub {
  font-size: 12px;
  color: var(--accent);
  text-decoration: none;
  transition: opacity var(--transition);
}

.pepic-header__hub:hover {
  text-decoration: underline;
}

.pepic-empty {
  font-size: 13px;
  color: var(--text-muted);
  padding: 12px 0;
  text-align: center;
}

.pepic-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px;
}

.pepic-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--bg-soft);
  text-decoration: none;
  transition:
    transform var(--transition),
    border-color var(--transition),
    box-shadow var(--transition);
}

.pepic-card:hover {
  transform: translateY(-2px);
  border-color: var(--accent);
  box-shadow: var(--shadow-sm);
}

.pepic-card--upcoming {
  border-style: dashed;
}

.pepic-card__media {
  position: relative;
  aspect-ratio: 16 / 9;
  background: var(--bg-card);
  overflow: hidden;
}

.pepic-card__img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform var(--transition);
}

.pepic-card:hover .pepic-card__img {
  transform: scale(1.03);
}

.pepic-card--upcoming .pepic-card__img {
  filter: saturate(0.75);
}

.pepic-card__media::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    rgba(0, 0, 0, 0) 50%,
    rgba(0, 0, 0, 0.55) 100%
  );
  pointer-events: none;
}

.pepic-card__fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  font-size: 13px;
  font-weight: 700;
  color: var(--text-muted);
}

.pepic-card__free {
  position: absolute;
  top: 6px;
  left: 6px;
  z-index: 1;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: var(--success);
  color: var(--text-on-fill);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
}

.pepic-card__free--ghost {
  background: transparent;
  border: 1px solid var(--success-on-dark);
  color: var(--success-on-dark);
}

.pepic-card__status {
  position: absolute;
  bottom: 5px;
  left: 6px;
  right: 6px;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}

.pepic-card__state {
  flex-shrink: 0;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--success-a15);
  color: var(--success-on-dark);
  font-size: 10px;
  font-weight: 600;
}

.pepic-card__status--upcoming .pepic-card__state {
  background: var(--accent-a15);
  color: var(--accent);
}

.pepic-card__state--mobile {
  background: var(--accent-a15);
  color: var(--accent);
}

.pepic-card__countdown {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-on-fill);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pepic-card__mark {
  position: absolute;
  top: 6px;
  right: 6px;
  z-index: 1;
  width: 14px;
  height: 14px;
  border-radius: 4px;
  background: var(--bg-card);
  padding: 2px;
  box-shadow: var(--shadow-sm);
}

.pepic-card__body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px 10px;
}

.pepic-card__title-row {
  display: flex;
  align-items: center;
  min-width: 0;
}

.pepic-card__name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pepic-card__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  font-size: 11px;
}

.pepic-card__start {
  color: var(--text-muted);
}

.pepic-card__action {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

.pepic-card__orig {
  color: var(--text-muted);
  text-decoration: line-through;
}

.pepic-card__claim {
  color: var(--accent);
  font-weight: 600;
}
</style>
