<script setup lang="ts">
/* 系列进度：按 `games.series_id` 聚合「拥有 / 已玩 / 白金 / 已玩成员完成度」。
 *
 * 与口味画像里的「系列时长排行」是两件事：那边答「哪个系列玩得最多」，
 * 这里答「每个系列我还差多少」。同一份分组数据的两个问法，故都留着。
 *
 * 口径（后端已把两个分母都吐出来，前端无需做二次除法）：
 * · 进度条 = 已玩成员口径（playedUnlocked / playedTotal）——含未玩成员会让
 *   9 款一款没碰的大系列恒显 0%，条形失去区分度；
 * · 副行同时给出整体账 `unlocked / total`，避免「只看已玩」把大系列读小。
 *
 * 展示名走后端 `seriesName`（成员展示名的最长公共汉字前缀，否则回落标识），
 * 与游戏详情页「同系列」区块同源——48 个多作品系列里 9 个因此显示成
 * 中文系列名（Legend Of Heroes → 英雄传说闪之轨迹、Maitetsu → 爱上火车）。 */
import { computed } from 'vue'

import type { CareerPayload } from '@/api/client'
import { HlIcon, HlImg, HlStat } from '@/components/ui'
import { useI18n } from '@/locales'
import { PALETTE } from '@/lib/familyColors'
import { vStagger } from '@/lib/stagger'
import { fmtHours, sheenStyle } from './careerUtil'

const props = defineProps<{ career: CareerPayload }>()

const { t } = useI18n()

/** 「刷到过半」的标记线：达标只挂静态徽章，不做动画（真实性优先） */
const HOT_PROGRESS = 0.5

const series = computed(() => props.career.series)

const rows = computed(() =>
  series.value.rows.map((r) => ({
    ...r,
    pct: Math.round(r.progress * 100),
    /** 全白金系列才是真正的「珍贵瞬间」：扫光只给这一档 */
    precious: r.owned >= 2 && r.platinum > 0 && r.platinum === r.owned,
    hot: r.playedTotal > 0 && r.progress >= HOT_PROGRESS,
    full: r.total > 0 ? Math.round((r.unlocked / r.total) * 100) : 0,
  })),
)

const stats = computed(() => [
  {
    key: 'totalLabel' as const,
    color: PALETTE.sky,
    value: String(series.value.seriesTotal),
  },
  {
    key: 'tagged' as const,
    color: PALETTE.teal,
    value: String(series.value.taggedTotal),
  },
  {
    key: 'perfected' as const,
    color: PALETTE.gold,
    value: String(series.value.perfected),
  },
])
</script>

<template>
  <section data-section="achievements.section.series" class="cr-stack">
    <header class="cr-sec-head">
      <h3>
        <HlIcon name="layers" />
        {{ t('achievements.section.series') }}
      </h3>
    </header>

    <div class="cr-card">
      <div class="cr-head">
        <span class="cr-head-title">
          <HlIcon name="layers" />
          {{ t('achievements.career.series.title') }}
        </span>
        <span class="cr-head-hint">
          {{
            t('achievements.career.series.hint', {
              n: series.seriesTotal,
              m: series.taggedTotal,
            })
          }}
        </span>
      </div>

      <div class="cr-grid3 cr-series-stats">
        <HlStat
          v-for="s in stats"
          :key="s.key"
          size="sm"
          :color="s.color"
          :style="{ borderColor: s.color + '30' }"
          :label="t(`achievements.career.series.${s.key}`)"
          :value="s.value"
        />
      </div>

      <div v-if="rows.length" class="cr-scroll hl-stagger" v-stagger>
        <article
          v-for="r in rows"
          :key="r.seriesId"
          class="cr-series"
          :class="{ 'is-precious': r.precious, 'is-hot': r.hot, 'cr-sheen': r.precious, 'cr-gilded': r.precious }"
          :style="sheenStyle(r.seriesId.length * 977 + r.owned)"
        >
          <div class="cr-series-cover-box">
            <HlImg class="cr-series-cover" :src="r.topGame?.headerImage" alt="" loading="lazy">
              <template #fallback>
                <div class="cr-series-fb">{{ r.name.slice(0, 1) }}</div>
              </template>
            </HlImg>
          </div>

          <div class="cr-series-body">
            <div class="cr-series-top">
              <span class="cr-series-name" :title="r.seriesId">{{ r.name }}</span>
              <span class="cr-pill hl-num">{{ t('achievements.career.series.owned', { n: r.owned }) }}</span>
              <span class="cr-pill hl-num">{{ t('achievements.career.series.played', { n: r.played }) }}</span>
              <span v-if="r.platinum > 0" class="cr-pill cr-pill--gold hl-num">
                {{ t('achievements.career.series.platinum', { n: r.platinum }) }}
              </span>
              <span v-if="r.completed > 0" class="cr-pill hl-num">
                {{ t('achievements.career.series.completed', { n: r.completed }) }}
              </span>
              <span v-if="r.hot" class="cr-pill cr-pill--hot">
                {{ t('achievements.career.series.hot') }}
              </span>
            </div>

            <div v-if="r.playedTotal > 0" class="cr-series-bar-row">
              <span class="cr-note cr-series-bar-label">
                {{ t('achievements.career.series.progressLabel') }}
              </span>
              <div class="cr-bar cr-series-bar">
                <i
                  :style="{
                    width: `${r.pct}%`,
                    background: r.precious ? PALETTE.gold : r.hot ? PALETTE.lime : PALETTE.sky,
                  }"
                />
              </div>
              <span class="cr-note hl-num">{{ t('achievements.career.series.progressValue', { unlocked: r.playedUnlocked, total: r.playedTotal }) }}</span>
            </div>
            <div v-else class="cr-series-bar-row">
              <span class="cr-note">{{ t('achievements.career.series.noProgress') }}</span>
            </div>

            <div class="cr-series-meta">
              <span class="hl-num cr-series-hours">{{ fmtHours(r.playtimeMin, t) }}</span>
              <span v-if="r.total > 0" class="cr-series-full hl-num">
                {{ t('achievements.career.series.progressValue', { unlocked: r.unlocked, total: r.total }) }}
              </span>
              <span v-if="r.nextGame" class="cr-series-next" :title="r.nextGame.name">
                <HlIcon name="target" />
                {{ r.nextGame.name }}
                <b class="hl-num">{{ t('achievements.career.series.next', { n: r.nextGame.remaining }) }}</b>
              </span>
            </div>
          </div>
        </article>
      </div>

      <p v-else class="cr-note cr-empty">{{ t('achievements.career.series.empty') }}</p>

      <p class="cr-note cr-series-foot">{{ t('achievements.career.series.footer') }}</p>
    </div>
  </section>
</template>

<style scoped>
.cr-series-stats {
  margin-bottom: 10px;
}

.cr-series {
  display: flex;
  gap: 9px;
  padding: 7px 8px;
  border: 1px solid var(--line-1);
  border-left: 3px solid var(--line-1);
  border-radius: var(--radius-sm);
  background: var(--surface-inset-sm);
  transition: transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.cr-series + .cr-series {
  margin-top: 6px;
}

.cr-series:hover {
  transform: translateY(-1px);
  border-color: var(--border-soft);
}

.cr-series.is-hot {
  border-left-color: var(--accent-fill);
}

.cr-series.is-precious {
  border-left-color: var(--gild-2);
  background: var(--surface-inset);
}

.cr-series-cover-box {
  flex: 0 0 auto;
  width: 74px;
  height: 35px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-track);
}

.cr-series-cover {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.cr-series-fb {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  font-size: 14px;
  font-weight: 700;
  color: var(--text-dim);
}

.cr-series-body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.cr-series-top {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
}

.cr-series-name {
  font-size: 12px;
  font-weight: 700;
  color: var(--text-primary);
  margin-right: 2px;
}

.cr-pill--gold {
  border-color: var(--gild-2);
  color: var(--gild-2);
}

.cr-pill--hot {
  border-color: var(--accent);
  color: var(--accent);
}

.cr-series-bar-row {
  display: flex;
  align-items: center;
  gap: 7px;
}

.cr-series-bar-label {
  flex: 0 0 auto;
  font-size: 9.5px;
}

.cr-series-bar {
  flex: 1 1 auto;
  height: 5px;
  min-width: 40px;
}

.cr-series-meta {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: 9.5px;
  color: var(--text-dim);
  min-width: 0;
}

.cr-series-hours {
  flex: 0 0 auto;
  color: var(--text-secondary);
}

.cr-series-full {
  flex: 0 0 auto;
}

.cr-series-next {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  margin-left: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

.cr-series-next b {
  color: var(--gild-2);
}

.cr-series-foot {
  margin-top: 8px;
  text-align: right;
}
</style>