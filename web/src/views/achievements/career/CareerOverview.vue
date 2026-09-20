<script setup lang="ts">
/* 生涯总览：生涯评分环 + 八项 KPI + 生涯陈列墙。
 *
 * 评分环是 SVG 手绘（不走 ECharts）：一个环 + 一段进度弧而已，用图表库要
 * 背整套 option 契约，收益为零。描边生长走 `stroke-dashoffset` 过渡——与
 * 图表白屏同源的坑（rAF 后台限流）在这里不存在：CSS transition 由合成器
 * 推进，不依赖 rAF 逐帧算。
 *
 * 评分制为五级阶梯（careerTitles.SCORE_LADDERS）：环心是总阶（五维最低阶），
 * 弧长是各维五阶全程进度按权重合成的连续值；脉冲只属于第五阶——环与
 * 维度条都只在满级时呼吸，其余时候安分。 */
import { computed, onMounted, ref } from 'vue'

import type { CareerPayload } from '@/api/client'
import type { CareerScoreDim } from '@/lib/careerTitles'
import { HlIcon, HlImg, HlStat } from '@/components/ui'
import TrophyMedal from '@/components/business/TrophyMedal.vue'
import { useI18n } from '@/locales'
import { careerScore, SCORE_LEVEL_KEYS } from '@/lib/careerTitles'
import { PALETTE } from '@/lib/familyColors'
import { vStagger } from '@/lib/stagger'
import { fmtHours, sheenStyle } from './careerUtil'
import PlayerTitleCard from './PlayerTitleCard.vue'

const props = defineProps<{ career: CareerPayload }>()

const { t } = useI18n()

/* ── 评分环 ──
 * 单弧连续生长：弧长 = 各维「五阶全程进度」按权重合成的连续值，不机械分段，
 * 环心数字仍是总阶（五维最低阶）。 */
const RING_R = 52
const RING_C = 2 * Math.PI * RING_R

const score = computed(() => careerScore(props.career))

/** 弧长偏移：权重进度 100% → 0（整圈），0% → 周长（空圈） */
const dashOffset = computed(() => RING_C * (1 - score.value.weighted))

/* 入场：先渲染空圈，下一帧再写目标偏移，transition 才有起止差可跑 */
const drawn = ref(false)
onMounted(() => {
  requestAnimationFrame(() => {
    drawn.value = true
  })
})

/** 阶位 → 具名色：阶越高越贵金属（模块级只存映射，渲染期取值） */
const LEVEL_COLORS = [PALETTE.slate, PALETTE.teal, PALETTE.blue, PALETTE.violet, PALETTE.gold]
const levelColor = computed(
  () => LEVEL_COLORS[Math.min(4, Math.max(0, score.value.level - 1))] ?? PALETTE.slate,
)

const levelKey = computed(
  () => SCORE_LEVEL_KEYS[Math.min(4, Math.max(0, score.value.level - 1))],
)

/* ── 维度行：数值按单位取整（小时/天/款取整、库值折元），满级不显示目标 ── */
const SCORE_KEY = 'achievements.career.title.score'

function fmtNum(v: number): string {
  return String(Math.round(v))
}

function dimRange(d: CareerScoreDim): string {
  if (d.unit === 'fen') {
    const a = Math.round(d.value / 100)
    return d.target === null
      ? t(`${SCORE_KEY}.rangeFenMax`, { a })
      : t(`${SCORE_KEY}.rangeFen`, { a, b: Math.round(d.target / 100) })
  }
  const unit =
    d.unit === 'hour'
      ? t(`${SCORE_KEY}.unit.h`)
      : d.unit === 'day'
        ? t(`${SCORE_KEY}.unit.d`)
        : t(`${SCORE_KEY}.unit.n`)
  const a = fmtNum(d.value)
  return d.target === null
    ? t(`${SCORE_KEY}.rangeMax`, { a, unit })
    : t(`${SCORE_KEY}.range`, { a, b: fmtNum(d.target), unit })
}

/* ── KPI 八卡 ── */
const kpi = computed(() => {
  const c = props.career
  return {
    playtime: fmtHours(c.playtime.totalMin, t),
    played: c.playtime.playedGames,
    platinum: c.platinum.count,
    rare: c.trophy.rareCount,
    avgRarity: `${c.trophy.avgRarity.toFixed(1)}%`,
    rate: `${c.trophy.rate.toFixed(1)}%`,
    activeDays: c.activity.activeDays,
    span: c.activity.spanDays,
    value: t('achievements.career.title.unit.fen', {
      n: (c.library.valueFen / 100).toFixed(0),
    }),
    costPerHour: t('achievements.career.title.unit.fenPerHour', {
      n: (c.library.costPerHourFen / 100).toFixed(2),
    }),
  }
})

/* ── 陈列墙（前 12 款，按游玩时长）── */
const SPOT_MAX = 12
const spots = computed(() =>
  props.career.spotlight.slice(0, SPOT_MAX).map((g) => ({
    ...g,
    pct: g.total > 0 ? Math.round((g.unlocked / g.total) * 100) : 0,
  })),
)

/** 维度序号 → 徽章色（与阶位色无关，只做行间区分） */
function dimColor(index: number): string {
  return [PALETTE.sky, PALETTE.gold, PALETTE.violet, PALETTE.lime, PALETTE.teal][index] ?? PALETTE.slate
}
</script>

<template>
  <section data-section="achievements.section.career" class="cr-stack">
    <header class="cr-sec-head">
      <h3>
        <HlIcon name="layers" />
        {{ t('achievements.section.career') }}
      </h3>
    </header>

    <!-- 玩家名片（头像 + 用户名 + 彩虹称号）：生涯侧写的门面，放在最前 -->
    <PlayerTitleCard :career="career" />

    <div class="cr-grid2">
      <!-- 生涯评分 -->
      <div class="cr-card cr-score" :style="{ '--cr-tier': tierColor }">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="zap" />
            {{ t('achievements.career.title.score.title') }}
          </span>
          <span class="cr-head-hint">{{ t('achievements.career.title.score.hint') }}</span>
        </div>
        <div class="cr-score-body">
          <div class="cr-ring-box">
            <svg class="cr-ring" viewBox="0 0 120 120" role="img" :aria-label="t('achievements.career.title.score.title')">
              <circle class="cr-ring-track" cx="60" cy="60" :r="RING_R" />
              <circle
                class="cr-ring-arc"
                :class="{ 'cr-ring-arc--pulse': score.maxed }"
                cx="60"
                cy="60"
                :r="RING_R"
                :stroke-dasharray="RING_C"
                :stroke-dashoffset="drawn ? dashOffset : RING_C"
              />
            </svg>
            <div class="cr-ring-center">
              <b class="cr-ring-num hl-num">{{ score.level }}</b>
              <span class="cr-ring-out">{{ t('achievements.career.title.score.levelOf') }}</span>
              <span class="cr-ring-tier">{{ t(levelKey) }}</span>
            </div>
          </div>
          <div class="cr-dims">
            <div v-for="(d, i) in score.dims" :key="d.id" class="cr-dim">
              <div class="cr-dim-top">
                <span class="cr-dim-name">{{ t(d.key) }}</span>
                <span class="cr-dim-val hl-num">{{ dimRange(d) }}</span>
                <span class="cr-dim-lv" :class="{ 'is-max': d.maxed }">
                  {{ t('achievements.career.title.score.lv', { n: d.level }) }}
                </span>
              </div>
              <div class="cr-bar" :class="{ 'cr-bar--pulse': d.maxed }">
                <i
                  :style="{ width: `${Math.round(d.progress * 100)}%`, background: dimColor(i), '--bar-c': dimColor(i) }"
                />
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 生涯 KPI -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="chart" />
            {{ t('achievements.career.ov.playtime') }}
          </span>
          <span class="cr-head-hint">
            {{ t('achievements.career.ov.spanSub') }}
          </span>
        </div>
        <div class="cr-kpi">
          <HlStat
            size="sm"
            :color="PALETTE.amber"
            :style="{ borderColor: PALETTE.amber + '30' }"
            :label="t('achievements.career.ov.played')"
            :value="String(kpi.played)"
            :sub="t('achievements.career.ov.playedSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.gold"
            :style="{ borderColor: PALETTE.gold + '30' }"
            :label="t('achievements.career.ov.platinum')"
            :value="String(kpi.platinum)"
            :sub="t('achievements.career.ov.platinumSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.violet"
            :style="{ borderColor: PALETTE.violet + '30' }"
            :label="t('achievements.career.ov.rare')"
            :value="String(kpi.rare)"
            :sub="t('achievements.career.ov.rareSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.pink"
            :style="{ borderColor: PALETTE.pink + '30' }"
            :label="t('achievements.career.ov.avgRarity')"
            :value="kpi.avgRarity"
            :sub="t('achievements.career.ov.avgRaritySub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.lime"
            :style="{ borderColor: PALETTE.lime + '30' }"
            :label="t('achievements.career.ov.rate')"
            :value="kpi.rate"
            :sub="t('achievements.career.ov.rateSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.teal"
            :style="{ borderColor: PALETTE.teal + '30' }"
            :label="t('achievements.career.ov.activeDays')"
            :value="String(kpi.activeDays)"
            :sub="t('achievements.career.ov.activeDaysSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.blue"
            :style="{ borderColor: PALETTE.blue + '30' }"
            :label="t('achievements.career.ov.value')"
            :value="kpi.value"
            :sub="t('achievements.career.ov.valueSub', { n: props.career.library.pricedGames })"
          />
          <HlStat
            size="sm"
            :color="PALETTE.orange"
            :style="{ borderColor: PALETTE.orange + '30' }"
            :label="t('achievements.career.ov.costPerHour')"
            :value="kpi.costPerHour"
            :sub="t('achievements.career.ov.costPerHourSub')"
          />
        </div>
      </div>
    </div>

    <!-- 生涯陈列墙：封面 + 时长 + 成就进度 -->
    <div class="cr-card">
      <div class="cr-head">
        <span class="cr-head-title">
          <HlIcon name="gamepad" />
          {{ t('achievements.career.spot.title') }}
        </span>
        <span class="cr-head-hint">
          {{ t('achievements.career.spot.hint', { n: spots.length }) }}
        </span>
      </div>
      <div v-if="spots.length" class="cr-spot-grid hl-stagger" v-stagger>
          <article
            v-for="g in spots"
            :key="g.appid"
            class="cr-spot"
            :class="{ 'cr-sheen': g.platinum, 'cr-gilded': g.platinum }"
            :style="sheenStyle(g.appid)"
          >
          <div class="cr-spot-cover-box">
            <HlImg :src="g.headerImage" class="cr-spot-cover" alt="" loading="lazy">
              <template #fallback><div class="cr-spot-fb">{{ g.name.slice(0, 1) }}</div></template>
            </HlImg>
            <span v-if="g.platinum" class="cr-spot-plat">
              <TrophyMedal tier="platinum" :size="16" />
              {{ t('achievements.career.spot.platinum') }}
            </span>
          </div>
          <div class="cr-spot-name" :title="g.name">{{ g.name }}</div>
          <div class="cr-spot-meta">
            <span class="hl-num">{{ fmtHours(g.playtimeMin, t) }}</span>
            <span class="hl-num">
              {{ t('achievements.career.spot.unlocked', { unlocked: g.unlocked, total: g.total }) }}
            </span>
          </div>
          <div class="cr-bar cr-spot-bar">
            <i :style="{ width: `${g.pct}%` }" />
          </div>
        </article>
      </div>
      <p v-else class="cr-note">{{ t('achievements.career.spot.empty') }}</p>
    </div>
  </section>
</template>

<style scoped>
/* 评分区：左环右维度 */
.cr-score-body {
  display: flex;
  align-items: center;
  gap: 14px;
}

.cr-ring-box {
  position: relative;
  flex: 0 0 auto;
  width: 132px;
  height: 132px;
  display: grid;
  place-items: center;
  border-radius: 50%;
}

.cr-ring {
  width: 132px;
  height: 132px;
  transform: rotate(-90deg);
}

.cr-ring-track {
  fill: none;
  stroke: var(--surface-track);
  stroke-width: 9;
}

.cr-ring-arc {
  fill: none;
  stroke: var(--cr-tier);
  stroke-width: 9;
  stroke-linecap: round;
  filter: drop-shadow(0 0 5px var(--cr-tier));
  transition: stroke-dashoffset calc(var(--duration-5) * var(--motion-scale) * 3) var(--ease-out);
}

.cr-ring-center {
  position: absolute;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
}

.cr-ring-num {
  font-size: 30px;
  line-height: 1;
  font-weight: 800;
  color: var(--cr-tier);
}

.cr-ring-out {
  font-size: 10px;
  color: var(--text-dim);
}

.cr-ring-tier {
  margin-top: 3px;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-primary);
}

/* 满级脉冲：环身呼吸（描边加粗 + 光晕涨落），只属于第五阶 */
.cr-ring-arc--pulse {
  animation: crRingPulse 2.4s ease-in-out infinite;
}

@keyframes crRingPulse {
  0%,
  100% {
    stroke-width: 9;
    filter: drop-shadow(0 0 5px var(--cr-tier));
  }

  50% {
    stroke-width: 12;
    filter: drop-shadow(0 0 13px var(--cr-tier));
  }
}

.cr-dims {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.cr-dim-top {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
}

.cr-dim-name {
  color: var(--text-primary);
  flex: 0 0 auto;
}

.cr-dim-val {
  color: var(--text-dim);
  font-size: 10px;
  flex: 1 1 auto;
  text-align: right;
}

.cr-dim-lv {
  flex: 0 0 auto;
  padding: 0 6px;
  border-radius: 999px;
  font-size: 9px;
  font-weight: 700;
  line-height: 15px;
  color: var(--text-secondary);
  background: var(--surface-inset);
  border: 1px solid var(--line-1);
}

.cr-dim-lv.is-max {
  color: var(--text-primary);
  border-color: var(--gild-2);
}

/* 满级维度条同款呼吸（亮度 + 光晕），脉冲只此一档 */
.cr-bar--pulse i {
  animation: crBarPulse 2.1s ease-in-out infinite;
}

@keyframes crBarPulse {
  0%,
  100% {
    filter: brightness(1);
    box-shadow: 0 0 0 0 transparent;
  }

  50% {
    filter: brightness(1.35);
    box-shadow: 0 0 7px 0 var(--bar-c, transparent);
  }
}

/* 循环动效红线：reduced-motion 下显式关停（0s + infinite 会空转） */
@media (prefers-reduced-motion: reduce) {
  .cr-ring-arc--pulse,
  .cr-bar--pulse i {
    animation: none;
  }
}

/* 陈列墙 */
.cr-spot-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(152px, 1fr));
  gap: 8px;
}

.cr-spot {
  display: flex;
  flex-direction: column;
  padding: 0 0 7px;
  border: 1px solid var(--line-1);
  border-radius: var(--radius-sm);
  background: var(--surface-inset);
  transition: transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.cr-spot:hover {
  transform: translateY(-2px);
  border-color: var(--border-soft);
}

.cr-spot-cover-box {
  position: relative;
  height: 72px;
  background: var(--surface-track);
  overflow: hidden;
}

.cr-spot-cover {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.cr-spot-fb {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  font-size: 22px;
  font-weight: 700;
  color: var(--text-dim);
}

.cr-spot-plat {
  position: absolute;
  right: 5px;
  bottom: 5px;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 9px;
  font-weight: 700;
  color: var(--text-primary);
  background: var(--surface-inset);
  border: 1px solid var(--gild-2);
}

.cr-spot-name {
  padding: 7px 7px 0;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-spot-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 2px 7px 0;
  font-size: 9.5px;
  color: var(--text-dim);
}

.cr-spot-bar {
  margin: 5px 7px 0;
  height: 3px;
}
</style>