<script setup lang="ts">
/* 珍藏馆：成就箴言 + 未完待续 + 尘封角落。
 *
 * 三块都属于「情绪面」而不是「统计面」：
 * · 箴言 = 最稀有成就是怎么写的（描述文本本身就是奖杯叙事的一部分）；
 * · 未完待续 = 差几枚就全成就的游戏（最有行动价值的一批）；
 * · 尘封角落 = 最久没启动的游戏（配合最后的启动日期）。
 *
 * 扫光只给「全服解锁率 < 4%」的箴言——箴言墙上一眼看得出哪几条是硬货。 */
import { computed } from 'vue'

import type { CareerPayload } from '@/api/client'
import { HlIcon, HlImg } from '@/components/ui'
import TrophyMedal from '@/components/business/TrophyMedal.vue'
import { useI18n } from '@/locales'
import { PALETTE } from '@/lib/familyColors'
import { vStagger } from '@/lib/stagger'
import { fmtDate, fmtHours, sheenStyle } from './careerUtil'

const props = defineProps<{ career: CareerPayload }>()

const { t } = useI18n()

/** 扫光门槛：全服解锁率低于此值才算「硬货」 */
const RARE_SHEEN_PCT = 4

const quotes = computed(() =>
  props.career.quotes.map((q) => ({
    ...q,
    pct:
      typeof q.globalPercent === 'number'
        ? t('achievements.career.vault.quotePct', { pct: q.globalPercent })
        : '',
    rare: typeof q.globalPercent === 'number' && q.globalPercent < RARE_SHEEN_PCT,
  })),
)

const unfinished = computed(() =>
  props.career.unfinished.map((g) => ({
    ...g,
    pct: g.total > 0 ? Math.round((g.unlocked / g.total) * 100) : 0,
  })),
)

const dormant = computed(() =>
  props.career.dormant.map((g) => ({
    ...g,
    lastText: g.lastPlayed > 0
      ? t('achievements.career.vault.dormantLast', { date: fmtDate(g.lastPlayed) })
      : t('achievements.career.vault.dormantEmpty'),
  })),
)
</script>

<template>
  <section data-section="achievements.section.vault" class="cr-stack">
    <header class="cr-sec-head">
      <h3>
        <HlIcon name="star" />
        {{ t('achievements.section.vault') }}
      </h3>
    </header>

    <!-- 成就箴言 -->
    <div class="cr-card">
      <div class="cr-head">
        <span class="cr-head-title">
          <HlIcon name="star" />
          {{ t('achievements.career.vault.quoteTitle') }}
        </span>
        <span class="cr-head-hint">{{ t('achievements.career.vault.quoteHint') }}</span>
      </div>
      <div v-if="quotes.length" class="cr-quote-grid hl-stagger" v-stagger>
        <article
          v-for="(q, i) in quotes"
          :key="`${q.appid}-${q.name}-${i}`"
          class="cr-quote"
          :class="{ 'cr-sheen': q.rare, 'cr-gilded': q.rare }"
          :style="sheenStyle(q.appid + i)"
        >
          <div class="cr-quote-icon-box">
            <HlImg class="cr-quote-icon" :src="q.icon" alt="" loading="lazy">
              <template #fallback><div class="cr-quote-fb"><TrophyMedal tier="platinum" :size="26" /></div></template>
            </HlImg>
          </div>
          <div class="cr-quote-body">
            <p class="cr-quote-text" :title="q.text">{{ q.text }}</p>
            <div class="cr-quote-meta">
              <span class="cr-quote-game" :title="q.gameName">{{ q.gameName }}</span>
              <span v-if="q.pct" class="cr-pill cr-pill--rare">{{ q.pct }}</span>
            </div>
            <span class="cr-quote-ach">{{ q.name }}</span>
          </div>
        </article>
      </div>
      <p v-else class="cr-note cr-empty">{{ t('achievements.career.vault.quoteEmpty') }}</p>
    </div>

    <div class="cr-grid2">
      <!-- 未完待续 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="target" />
            {{ t('achievements.career.vault.unfinishedTitle') }}
          </span>
          <span class="cr-head-hint">
            {{ t('achievements.career.vault.unfinishedTotal', { n: props.career.unfinishedCount }) }}
          </span>
        </div>
        <div v-if="unfinished.length" class="cr-scroll">
          <div v-for="g in unfinished" :key="g.appid" class="cr-uf">
            <HlImg class="cr-uf-cover" :src="g.headerImage" alt="" loading="lazy">
              <template #fallback><div class="cr-uf-fb">{{ g.name.slice(0, 1) }}</div></template>
            </HlImg>
            <div class="cr-uf-body">
              <div class="cr-uf-top">
                <span class="cr-uf-name" :title="g.name">{{ g.name }}</span>
                <span class="cr-pill cr-pill--fast">
                  {{ t('achievements.career.vault.unfinishedRemain', { n: g.remaining }) }}
                </span>
              </div>
              <div class="cr-bar">
                <i :style="{ width: `${g.pct}%`, background: PALETTE.lime }" />
              </div>
              <div class="cr-uf-meta hl-num">
                {{ t('achievements.career.vault.progress', { unlocked: g.unlocked, total: g.total }) }}
                <span>{{ fmtHours(g.playtimeMin, t) }}</span>
              </div>
            </div>
          </div>
        </div>
        <p v-else class="cr-note cr-empty">
          {{ t('achievements.career.vault.unfinishedEmpty') }}
        </p>
      </div>

      <!-- 尘封角落 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="moon" />
            {{ t('achievements.career.vault.dormantTitle') }}
          </span>
          <span class="cr-head-hint">{{ t('achievements.career.vault.dormantHint') }}</span>
        </div>
        <div v-if="dormant.length" class="cr-scroll">
          <div v-for="g in dormant" :key="g.appid" class="cr-uf">
            <HlImg class="cr-uf-cover" :src="g.headerImage" alt="" loading="lazy">
              <template #fallback><div class="cr-uf-fb">{{ g.name.slice(0, 1) }}</div></template>
            </HlImg>
            <div class="cr-uf-body">
              <div class="cr-uf-top">
                <span class="cr-uf-name" :title="g.name">{{ g.name }}</span>
                <span class="cr-pill hl-num">{{ fmtHours(g.playtimeMin, t) }}</span>
              </div>
              <div class="cr-uf-meta hl-num">
                <span>{{ g.lastText }}</span>
                <span>
                  {{ t('achievements.career.vault.progress', { unlocked: g.unlocked, total: g.total }) }}
                </span>
              </div>
            </div>
          </div>
        </div>
        <p v-else class="cr-note cr-empty">
          {{ t('achievements.career.vault.dormantEmpty') }}
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 箴言墙：图标 + 描述文本 + 出处；描述本身是成就原文，故给足行距与上限行数 */
.cr-quote-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(228px, 1fr));
  gap: 8px;
}

.cr-quote {
  display: flex;
  gap: 8px;
  padding: 9px 10px;
  border: 1px solid var(--line-1);
  border-left: 3px solid var(--line-1);
  border-radius: var(--radius-sm);
  background: var(--surface-inset-sm);
  transition: transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.cr-quote:hover {
  transform: translateY(-2px);
  border-color: var(--border-soft);
}

.cr-quote-icon-box {
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-track);
}

.cr-quote-icon {
  width: 34px;
  height: 34px;
  object-fit: cover;
  display: block;
}

.cr-quote-fb {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  font-size: 16px;
}

.cr-quote-body {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.cr-quote-text {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--text-primary);
  /* 描述最长 140 字上下，超过三行会压垮整面墙的节奏 */
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.cr-quote-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 9.5px;
  color: var(--text-dim);
  min-width: 0;
}

.cr-quote-game {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-quote-ach {
  font-size: 9.5px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-pill--rare {
  border-color: var(--gild-2);
  color: var(--gild-2);
  flex: 0 0 auto;
}

/* 未完待续 / 尘封角落：同一行骨架 */
.cr-uf {
  display: flex;
  gap: 8px;
  padding: 5px 0;
  align-items: center;
}

.cr-uf-cover {
  flex: 0 0 auto;
  width: 58px;
  height: 27px;
  border-radius: 4px;
  object-fit: cover;
  display: block;
  background: var(--surface-track);
}

.cr-uf-fb {
  width: 58px;
  height: 27px;
  border-radius: 4px;
  display: grid;
  place-items: center;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-dim);
  background: var(--surface-track);
}

.cr-uf-body {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.cr-uf-top {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
}

.cr-uf-name {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-uf-meta {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 9.5px;
  color: var(--text-dim);
}
</style>