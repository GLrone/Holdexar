<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PilotPriceFacts } from '@/api/client'
import { useI18n } from '@/locales'
import { pilotCoverUrl, ratingClass } from '@/lib/pilotView'
import HlImg from '@/components/ui/HlImg.vue'
import HlPilotTrend from '@/components/business/HlPilotTrend.vue'
import {
  dateOf,
  fen,
  fmtCount,
  getDealAdvice,
  medianPos,
  rangePos,
  ratingText,
} from '../helpers'

const props = defineProps<{
  data: PilotPriceFacts
}>()

const { t, locale } = useI18n()
const c = computed(() => props.data)

const trendExpanded = ref(false)
function toggleTrend() {
  trendExpanded.value = !trendExpanded.value
}
</script>

<template>
  <div class="hsui-price-card">
    <div class="pcard-hero">
      <HlImg :src="pilotCoverUrl(c.appid)" :alt="c.name || `AppID ${c.appid}`" loading="lazy" class="pcard-hero__cover">
        <template #fallback>
          <span class="pcard-hero__cover-fallback">{{ (c.name || `AppID ${c.appid}`).slice(0, 2) }}</span>
        </template>
      </HlImg>

      <div class="pcard-hero__info">
        <div class="pcard-hero__title-row">
          <router-link class="pcard-hero__title" :to="`/game/${c.appid}`" :title="c.name || `AppID ${c.appid}`">
            {{ c.name || `AppID ${c.appid}` }}
            <svg class="pcard-hero__title-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M7 17l9.2-9.2M17 17V8H8" />
            </svg>
          </router-link>
        </div>

        <div class="pcard-hero__badges">
          <span
            v-if="ratingText(c.positiveRate)"
            class="pcard-pill pcard-pill--rating"
            :class="ratingClass(c.positiveRate)"
          >
            <i class="pcard-pill__dot" aria-hidden="true"></i>
            {{ t('pilot.card.rating', { rate: ratingText(c.positiveRate) }) }}
            <template v-if="c.reviewCount"> · {{ fmtCount(c.reviewCount, locale) }}</template>
          </span>

          <span
            v-if="getDealAdvice(c)"
            class="pcard-pill pcard-pill--advice"
            :class="`is-${getDealAdvice(c)!.tone}`"
          >
            <i class="pcard-pill__spark" aria-hidden="true"></i>
            {{ t(getDealAdvice(c)!.labelKey) }}
          </span>
        </div>
      </div>
    </div>

    <!-- 价格核心展示区 -->
    <div class="pcard-price-board">
      <div class="pcard-price-board__primary">
        <div class="pcard-price-board__now">
          <span class="pcard-price-board__currency">¥</span>
          <span class="pcard-price-board__val">{{ fen(c.cn?.cnyFen).replace(/[¥￥]/g, '') }}</span>
        </div>
        <span v-if="c.cn?.discount" class="pcard-disc-tag">
          -{{ c.cn.discount }}%
        </span>
      </div>

      <div v-if="c.cn?.cnyFen == null && c.alt" class="pcard-price-board__alt">
        <span class="pcard-price-board__alt-label">{{ t('pilot.card.altRegion', { region: c.alt.region }) }}</span>
        <span class="pcard-price-board__alt-val">{{ fen(c.alt.cnyFen) }}</span>
        <span v-if="c.alt.discount" class="pcard-disc-tag pcard-disc-tag--sm">-{{ c.alt.discount }}%</span>
      </div>
    </div>

    <!-- 历史价格基准指标面板 -->
    <div class="pcard-metrics-grid">
      <div class="pcard-metric-card">
        <span class="pcard-metric-card__k">{{ t('pilot.card.atl') }}</span>
        <span class="pcard-metric-card__v is-highlight">{{ fen(c.lowest?.cnyFen) }}</span>
        <span v-if="dateOf(c.lowest?.snapshotAt)" class="pcard-metric-card__sub">{{ dateOf(c.lowest?.snapshotAt) }}</span>
      </div>

      <div v-if="c.year" class="pcard-metric-card">
        <span class="pcard-metric-card__k">{{ t('pilot.card.medianTick') }}</span>
        <span class="pcard-metric-card__v">{{ fen(c.year.medianFen) }}</span>
        <span class="pcard-metric-card__sub">{{ t('pilot.card.trendRange', { min: fen(c.year.minFen), max: fen(c.year.maxFen) }) }}</span>
      </div>

      <div v-if="c.alt && c.cn?.cnyFen != null" class="pcard-metric-card">
        <span class="pcard-metric-card__k">{{ t('pilot.card.advice.altOnly') }}</span>
        <span class="pcard-metric-card__v">{{ fen(c.alt.cnyFen) }}</span>
        <span class="pcard-metric-card__sub">{{ c.alt.region }}<template v-if="c.alt.discount"> · -{{ c.alt.discount }}%</template></span>
      </div>
    </div>

    <!-- 价格水位区间标尺 -->
    <div v-if="rangePos(c) !== null" class="pcard-gauge">
      <div class="pcard-gauge__header">
        <span class="pcard-gauge__tip">{{ fen(c.year?.minFen) }}</span>
        <span class="pcard-gauge__tip">{{ fen(c.year?.maxFen) }}</span>
      </div>
      <div class="pcard-gauge__track">
        <i
          class="pcard-gauge__tick"
          :style="{ left: `${medianPos(c)}%` }"
          :title="t('pilot.card.medianTick')"
        ></i>
        <i class="pcard-gauge__dot" :style="{ left: `${rangePos(c)}%` }"></i>
      </div>
    </div>

    <!-- 折叠价格走势与统计说明 -->
    <div v-if="(c.trend && c.trend.length >= 2) || c.year" class="pcard-trend-wrap">
      <button
        type="button"
        class="pcard-trend-toggle"
        :aria-expanded="trendExpanded"
        @click="toggleTrend"
      >
        <svg class="pcard-trend-toggle__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="23 6 13.5 15.5 8.5 10.5 1 18" />
          <polyline points="17 6 23 6 23 12" />
        </svg>
        <span>{{ t(trendExpanded ? 'pilot.card.hideTrend' : 'pilot.card.toggleTrend') }}</span>
        <svg class="pcard-trend-toggle__chevron" :class="{ 'is-open': trendExpanded }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      <div v-if="trendExpanded" class="pcard-trend-content">
        <HlPilotTrend v-if="c.trend && c.trend.length >= 2" :points="c.trend" />
        <div v-if="c.year" class="pcard-trend-footnote">
          {{ t('pilot.card.year', { min: fen(c.year.minFen), max: fen(c.year.maxFen), median: fen(c.year.medianFen) }) }}
          <template v-if="c.year.count"> · {{ t('pilot.card.obs', { count: c.year.count }) }}</template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hsui-price-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 100%;
}

.pcard-hero {
  display: flex;
  gap: 12px;
  align-items: center;
}

.pcard-hero__cover {
  width: 90px;
  height: 48px;
  border-radius: var(--radius-sm, 6px);
  object-fit: cover;
  flex-shrink: 0;
  background: var(--surface-card-2, #2a475e);
}

.pcard-hero__cover-fallback {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 700;
  color: var(--text-muted, #8f98a0);
}

.pcard-hero__info {
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow: hidden;
  flex: 1;
}

.pcard-hero__title-row {
  display: flex;
  align-items: center;
}

.pcard-hero__title {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary, #d9e3ec);
  text-decoration: none;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pcard-hero__title:hover {
  color: var(--accent, #66c0f4);
}

.pcard-hero__title-arrow {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
}

.pcard-hero__badges {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.pcard-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px;
  font-size: 11px;
  border-radius: var(--radius-sm, 6px);
  font-weight: 500;
}

.pcard-pill--rating.is-rate-good {
  color: var(--rate-good, #2ecc71);
  background: rgba(46, 204, 113, 0.12);
}

.pcard-pill--rating.is-rate-mid {
  color: var(--rate-mid, #f1c40f);
  background: rgba(241, 196, 15, 0.12);
}

.pcard-pill--rating.is-rate-low {
  color: var(--rate-low, #e74c3c);
  background: rgba(231, 76, 60, 0.12);
}

.pcard-pill__dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.pcard-pill--advice.is-good {
  color: var(--success, #a4d007);
  background: var(--success-a15, rgba(164, 208, 7, 0.15));
}

.pcard-pill--advice.is-warn {
  color: var(--warning, #f59e0b);
  background: var(--warning-a40, rgba(245, 158, 11, 0.2));
}

.pcard-pill--advice.is-accent {
  color: var(--accent, #66c0f4);
  background: var(--accent-a15, rgba(102, 192, 244, 0.15));
}

.pcard-price-board {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 6px 0;
}

.pcard-price-board__primary {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.pcard-price-board__now {
  display: flex;
  align-items: baseline;
  color: var(--text-primary, #d9e3ec);
}

.pcard-price-board__currency {
  font-size: 16px;
  font-weight: 600;
  margin-right: 2px;
}

.pcard-price-board__val {
  font-size: 26px;
  font-weight: 700;
  line-height: 1;
}

.pcard-disc-tag {
  display: inline-flex;
  padding: 2px 6px;
  background: var(--success, #a4d007);
  color: var(--ink-on-fill);
  font-size: 12px;
  font-weight: 700;
  border-radius: 4px;
}

.pcard-metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 8px;
}

.pcard-metric-card {
  display: flex;
  flex-direction: column;
  padding: 8px;
  background: var(--surface-card-2, #2a475e);
  border-radius: var(--radius-sm, 6px);
  gap: 2px;
}

.pcard-metric-card__k {
  font-size: 10px;
  color: var(--text-muted, #8f98a0);
}

.pcard-metric-card__v {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-secondary, #c7d5e0);
}

.pcard-metric-card__v.is-highlight {
  color: var(--success, #a4d007);
}

.pcard-metric-card__sub {
  font-size: 10px;
  color: var(--text-dim, #5a7080);
}

.pcard-gauge {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 4px;
}

.pcard-gauge__header {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-dim, #5a7080);
}

.pcard-gauge__track {
  position: relative;
  height: 4px;
  background: var(--surface-track, rgba(255, 255, 255, 0.12));
  border-radius: 2px;
}

.pcard-gauge__tick {
  position: absolute;
  top: -2px;
  width: 2px;
  height: 8px;
  background: var(--text-dim, #5a7080);
}

.pcard-gauge__dot {
  position: absolute;
  top: -4px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--accent, #66c0f4);
  transform: translateX(-50%);
  box-shadow: 0 0 6px var(--accent-a50, rgba(102, 192, 244, 0.5));
}

.pcard-trend-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-top: 1px solid var(--border-soft, rgba(102, 192, 244, 0.1));
  padding-top: 8px;
}

.pcard-trend-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  background: transparent;
  border: none;
  font-size: 11px;
  color: var(--text-muted, #8f98a0);
  cursor: pointer;
  padding: 0;
}

.pcard-trend-toggle:hover {
  color: var(--text-primary, #d9e3ec);
}

.pcard-trend-toggle__icon {
  width: 13px;
  height: 13px;
}

.pcard-trend-toggle__chevron {
  width: 12px;
  height: 12px;
  margin-left: auto;
  transition: transform var(--transition, 0.22s ease);
}

.pcard-trend-toggle__chevron.is-open {
  transform: rotate(180deg);
}

.pcard-trend-footnote {
  font-size: 11px;
  color: var(--text-dim, #5a7080);
  margin-top: 4px;
  text-align: right;
}
</style>
