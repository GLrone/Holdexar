<script setup lang="ts">
/* 账户称号墙：一系度量一张名片，五级阶梯逐级升级（lib/careerTitles.ts）。
 *
 * 这里只做呈现与筛选：分类（时长/奖杯/白金/稀有/活跃/偏好/库藏）×
 * 状态（全部/已入阶/未入阶）。
 *
 * 动效分级：**鎏金（五阶满）是唯一配流动金边 + 扫光的档位**；传说/史诗等
 * 其余档位只有档色、不挂扫光——「珍贵」要有对比度，满墙都亮等于都不亮。 */
import { computed, ref } from 'vue'

import type { CareerPayload } from '@/api/client'
import { HlEmpty, HlIcon, HlPaneSwitch, HlSegmented, type HlSelectOption } from '@/components/ui'
import type { MessageKey } from '@/locales'
import { useI18n } from '@/locales'
import {
  CARD_TIER_ORDER,
  CATEGORY_ORDER,
  evaluateTitleCards,
  formatMetric,
  type CareerCardTier,
  type CareerTitleCard,
  type CareerTitleCategory,
} from '@/lib/careerTitles'
import { PALETTE } from '@/lib/familyColors'
import { vStagger } from '@/lib/stagger'
import { sheenStyle } from './careerUtil'

const props = defineProps<{ career: CareerPayload }>()

const { t } = useI18n()

const CAT_KEY: Record<CareerTitleCategory, MessageKey> = {
  playtime: 'achievements.career.titles.category.playtime',
  trophy: 'achievements.career.titles.category.trophy',
  platinum: 'achievements.career.titles.category.platinum',
  rarity: 'achievements.career.titles.category.rarity',
  activity: 'achievements.career.titles.category.activity',
  taste: 'achievements.career.titles.category.taste',
  library: 'achievements.career.titles.category.library',
}

const TIER_KEY: Record<CareerCardTier, MessageKey> = {
  gilded: 'achievements.career.titles.tier.gilded',
  legendary: 'achievements.career.titles.tier.legendary',
  epic: 'achievements.career.titles.tier.epic',
  rare: 'achievements.career.titles.tier.rare',
  common: 'achievements.career.titles.tier.common',
}

/** 档位 → 具名色：鎏金用专门的鎏金 token，与其余档拉开质感差 */
const TIER_COLOR: Record<CareerCardTier, string> = {
  gilded: 'var(--gild-2)',
  legendary: PALETTE.gold,
  epic: PALETTE.violet,
  rare: PALETTE.blue,
  common: PALETTE.slate,
}

const all = computed(() => evaluateTitleCards(props.career))

const cat = ref<string>('all')
const state = ref<string>('all')

const catOptions = computed<HlSelectOption[]>(() => [
  { label: t('achievements.career.titles.catAll'), value: 'all' },
  ...CATEGORY_ORDER.map((c) => ({ label: t(CAT_KEY[c]), value: c })),
])

const stateOptions = computed<HlSelectOption[]>(() => [
  { label: t('achievements.career.titles.filter.all'), value: 'all' },
  { label: t('achievements.career.titles.filter.earned'), value: 'earned' },
  { label: t('achievements.career.titles.filter.locked'), value: 'locked' },
])

const inRankCount = computed(() => all.value.filter((x) => x.level >= 1).length)

const tierCount = computed(() => {
  const out: Record<CareerCardTier, number> = { gilded: 0, legendary: 0, epic: 0, rare: 0, common: 0 }
  for (const x of all.value) if (x.tier) out[x.tier] += 1
  return out
})

const shown = computed(() =>
  all.value
    .filter((x) => cat.value === 'all' || x.category === cat.value)
    .filter(
      (x) =>
        state.value === 'all' ||
        (state.value === 'earned' ? x.level >= 1 : x.level === 0),
    ),
)

/** 切换筛选时整块重排：交给 HlPaneSwitch 做方向统一的出入场 */
const paneKey = computed(() => `${cat.value}:${state.value}`)

/** 鎏金档（五阶满）独享：流动金边 + 扫光 */
function isGilded(x: CareerTitleCard): boolean {
  return x.maxed
}
</script>

<template>
  <section data-section="achievements.section.titles" class="cr-stack">
    <div class="cr-card">
      <div class="cr-head">
        <span class="cr-head-title">
          <HlIcon name="star" />
          {{ t('achievements.career.titles.title') }}
        </span>
        <span class="cr-head-hint">
          {{ t('achievements.career.titles.hint', { earned: inRankCount, total: all.length }) }}
        </span>
      </div>

      <!-- 档位总览：名片按当前档位分布 -->
      <div class="cr-title-tiers">
        <span
          v-for="tier in CARD_TIER_ORDER"
          :key="tier"
          class="cr-title-tier-chip"
          :style="{ borderColor: TIER_COLOR[tier] }"
        >
          <i class="cr-title-tier-dot" :style="{ background: TIER_COLOR[tier] }" />
          {{ t(TIER_KEY[tier]) }}
          <b class="hl-num">{{ tierCount[tier] }}</b>
        </span>
        <span class="cr-title-tier-total hl-num">
          {{ t('achievements.career.titles.summary', {
            gilded: tierCount.gilded,
            legendary: tierCount.legendary,
            epic: tierCount.epic,
            rare: tierCount.rare,
            common: tierCount.common,
          }) }}
        </span>
      </div>

      <!-- 筛选：分类 × 状态 -->
      <div class="cr-title-filters">
        <HlSegmented v-model="cat" :options="catOptions" />
        <HlSegmented v-model="state" :options="stateOptions" />
      </div>

      <HlPaneSwitch :pane-key="paneKey">
        <div v-if="shown.length" class="cr-title-grid hl-stagger" v-stagger>
          <article
            v-for="x in shown"
            :key="x.id"
            class="cr-title"
            :class="{
              'is-earned': x.level >= 1,
              'cr-gilded': isGilded(x),
              'cr-sheen': isGilded(x),
            }"
            :style="{ '--cr-tone': x.tier ? TIER_COLOR[x.tier] : 'var(--line-1)', ...sheenStyle(x.id.length * 131 + x.level) }"
          >
            <div class="cr-title-top">
              <span class="cr-title-icon"><HlIcon :name="x.icon" /></span>
              <span class="cr-title-tier">{{ x.tier ? t(TIER_KEY[x.tier]) : t('achievements.career.titles.unranked') }}</span>
            </div>
            <div class="cr-title-name">{{ t(x.nameKey) }}</div>
            <div class="cr-title-scope">{{ t(x.scopeKey) }}</div>
            <div class="cr-title-values">
              <span class="cr-pill">{{ formatMetric(x.value, x.unit, t) }}</span>
              <span v-if="x.target !== null" class="cr-pill cr-pill--dim">
                {{ t('achievements.career.titles.target', { value: formatMetric(x.target, x.unit, t) }) }}
              </span>
              <span v-else class="cr-pill cr-pill--dim">{{ t('achievements.career.titles.maxed') }}</span>
            </div>
            <div class="cr-bar cr-title-bar">
              <i
                :style="{ width: `${Math.round(x.progress * 100)}%`, background: x.tier ? TIER_COLOR[x.tier] : 'var(--surface-track)', '--bar-c': x.tier ? TIER_COLOR[x.tier] : 'transparent' }"
              />
            </div>
            <div class="cr-title-foot">
              <span class="cr-title-state" :class="{ 'is-on': x.level >= 1 }">
                {{ t('achievements.career.title.score.lv', { n: x.level }) }}
              </span>
              <span class="hl-num cr-title-pct">
                {{ t('achievements.career.titles.progress', { pct: Math.round(x.progress * 100) }) }}
              </span>
            </div>
          </article>
        </div>
        <HlEmpty v-else size="sm" icon="" :text="t('achievements.career.titles.empty')" />
      </HlPaneSwitch>
    </div>
  </section>
</template>

<style scoped>
.cr-title-tiers {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.cr-title-tier-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: 1px solid var(--line-1);
  border-radius: 999px;
  font-size: 10.5px;
  color: var(--text-secondary);
}

.cr-title-tier-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.cr-title-tier-total {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-dim);
}

.cr-title-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.cr-title-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  gap: 8px;
}

.cr-title {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 9px 10px 8px;
  border: 1px solid var(--line-1);
  border-left: 3px solid var(--surface-track);
  border-radius: var(--radius-sm);
  background: var(--surface-inset-sm);
  /* 未入阶：整体压暗一档，让已入阶的那批自然浮上来 */
  opacity: 0.62;
  transition: transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    opacity calc(var(--duration-3) * var(--motion-scale)) var(--ease-out),
    border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.cr-title.is-earned {
  opacity: 1;
  border-left-color: var(--cr-tone);
  background: var(--surface-inset);
}

.cr-title:hover {
  transform: translateY(-2px);
  border-color: var(--cr-tone);
}

.cr-title-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.cr-title-icon {
  display: inline-flex;
  color: var(--cr-tone);
}

.cr-title-tier {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--cr-tone);
}

.cr-title-name {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--text-primary);
}

.cr-title-scope {
  font-size: 10px;
  color: var(--text-dim);
  min-height: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-title-values {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.cr-pill--dim {
  color: var(--text-dim);
}

.cr-title-bar {
  height: 4px;
}

.cr-title-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  font-size: 9.5px;
  color: var(--text-dim);
}

.cr-title-state.is-on {
  color: var(--cr-tone);
  font-weight: 700;
}

.cr-title-pct {
  font-size: 9.5px;
}
</style>
