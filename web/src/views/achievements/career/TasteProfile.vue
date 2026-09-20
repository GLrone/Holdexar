<script setup lang="ts">
/* 口味画像：类型偏好 + 类型雷达 + 厂牌/系列排行 + 年代构成 + 偏好标签。
 *
 * 雷达图手绘 SVG：六个轴 + 四条刻度环 + 一个数据多边形。用 ECharts 的 radar
 * 需要背整套 option 契约（animation / tooltip 主题化）且这个图形没有任何交互，
 * 纯几何用 SVG 表达更短也更稳。
 *
 * 口径提醒：游戏在库里有多个类型，故**各类型游玩时长之和远大于总时长**——
 * 条形一律按「该类型内部排行」的相对长度画，不写「占总时长 x%」这种会
 * 加起来超过 100% 的假百分比。 */
import { computed } from 'vue'

import type { CareerPayload, CareerTasteRow } from '@/api/client'
import { HlIcon, HlImg, HlStat } from '@/components/ui'
import { useI18n } from '@/locales'
import { PALETTE } from '@/lib/familyColors'
import { vStagger } from '@/lib/stagger'
import { fmtHours } from './careerUtil'

const props = defineProps<{ career: CareerPayload }>()

const { t } = useI18n()

const taste = computed(() => props.career.taste)

/* ── 类型偏好（条形榜）── */
const GENRE_COLORS = [PALETTE.sky, PALETTE.blue, PALETTE.violet, PALETTE.pink, PALETTE.orange, PALETTE.teal, PALETTE.lime, PALETTE.iris]
const GENRE_MAX = 8

const genres = computed(() => {
  const list = taste.value.genres.slice(0, GENRE_MAX)
  const max = Math.max(1, ...list.map((g) => g.playtimeMin))
  return list.map((g, i) => ({
    ...g,
    pct: Math.round((g.playtimeMin / max) * 100),
    color: GENRE_COLORS[i % GENRE_COLORS.length],
  }))
})

/* ── 类型雷达（SVG 手绘）── */
const RADAR_R = 82
const RADAR_CX = 104
const RADAR_CY = 104

const radar = computed(() => {
  const list = taste.value.genres.slice(0, 6)
  const n = list.length
  if (n < 3) return null
  const max = Math.max(1, ...list.map((g) => g.playtimeMin))
  const step = (Math.PI * 2) / n
  const at = (i: number, ratio: number) => {
    const a = -Math.PI / 2 + i * step
    return {
      x: RADAR_CX + RADAR_R * ratio * Math.cos(a),
      y: RADAR_CY + RADAR_R * ratio * Math.sin(a),
    }
  }
  const rings = [0.25, 0.5, 0.75, 1].map((ratio) =>
    Array.from({ length: n }, (_, i) => {
      const p = at(i, ratio)
      return `${p.x.toFixed(1)},${p.y.toFixed(1)}`
    }).join(' '),
  )
  const spokes = list.map((_, i) => at(i, 1))
  const dots = list.map((g, i) => ({ ...at(i, g.playtimeMin / max), genre: g.genre, games: g.games }))
  return {
    n,
    rings,
    spokes,
    dots,
    shape: dots.map((d) => `${d.x.toFixed(1)},${d.y.toFixed(1)}`).join(' '),
    labels: list.map((g, i) => {
      const p = at(i, 1.16)
      return { x: p.x, y: p.y, text: g.genre }
    }),
  }
})

/* ── 厂牌 / 系列 ── */
function topRows(rows: CareerTasteRow[], limit: number) {
  const max = Math.max(1, ...rows.slice(0, limit).map((r) => r.playtimeMin))
  return rows.slice(0, limit).map((r) => ({
    name: r.name ?? r.genre ?? '',
    games: r.games,
    playtimeMin: r.playtimeMin,
    platinum: r.platinum,
    pct: Math.round((r.playtimeMin / max) * 100),
  }))
}

const developers = computed(() => topRows(taste.value.developers, 5))
const publishers = computed(() => topRows(taste.value.publishers, 5))
const series = computed(() => topRows(taste.value.series, 5))

/* ── 年代构成 ── */
const decades = computed(() => {
  const list = taste.value.decades
  const total = Math.max(1, list.reduce((s, d) => s + d.games, 0))
  const colors = [PALETTE.slate, PALETTE.blue, PALETTE.gold]
  return list.map((d, i) => ({
    ...d,
    pct: Math.round((d.games / total) * 100),
    color: colors[i % colors.length],
  }))
})

/* ── 偏好标签 ── */
const tags = computed(() => {
  const c = props.career
  const played = Math.max(1, c.playtime.playedGames)
  return {
    chinese: taste.value.chineseGames,
    chinesePct: Math.round((taste.value.chineseGames / played) * 100),
    fresh: taste.value.freshGames,
    avgYear: taste.value.avgReleaseYear,
    positive: Math.round(c.library.avgPositiveRate * 10) / 10,
  }
})

const oldest = computed(() => taste.value.oldestGame)
const newest = computed(() => taste.value.newestGame)
</script>

<template>
  <section data-section="achievements.section.taste" class="cr-stack">
    <div class="cr-grid2">
      <!-- 类型偏好 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="layers" />
            {{ t('achievements.career.taste.genreTitle') }}
          </span>
          <span class="cr-head-hint">{{ t('achievements.career.taste.genreHint') }}</span>
        </div>
        <div v-if="genres.length" class="hl-stagger" v-stagger>
          <div v-for="g in genres" :key="g.genre" class="cr-rank">
            <div class="cr-rank-top">
              <span class="cr-rank-name">{{ g.genre }}</span>
              <span class="cr-rank-meta">
                <span class="hl-num">{{ fmtHours(g.playtimeMin, t) }}</span>
                <span>{{ t('achievements.career.taste.games', { n: g.games }) }}</span>
                <span v-if="g.platinum > 0" class="cr-pill cr-pill--plat">
                  {{ t('achievements.career.taste.platinum', { n: g.platinum }) }}
                </span>
              </span>
            </div>
            <div class="cr-bar">
              <i :style="{ width: `${g.pct}%`, background: g.color }" />
            </div>
          </div>
        </div>
        <p v-else class="cr-note cr-empty">{{ t('achievements.career.taste.empty') }}</p>
      </div>

      <!-- 类型雷达 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="target" />
            {{ t('achievements.career.taste.radarTitle') }}
          </span>
          <span class="cr-head-hint">
            {{ t('achievements.career.taste.radarHint', { n: radar ? radar.n : 0 }) }}
          </span>
        </div>
        <div v-if="radar" class="cr-radar-box">
          <svg class="cr-radar" viewBox="0 0 208 208" role="img" :aria-label="t('achievements.career.taste.radarTitle')">
            <polygon
              v-for="(ring, i) in radar.rings"
              :key="`r${i}`"
              class="cr-radar-ring"
              :points="ring"
            />
            <line
              v-for="(s, i) in radar.spokes"
              :key="`s${i}`"
              class="cr-radar-spoke"
              :x1="RADAR_CX"
              :y1="RADAR_CY"
              :x2="s.x"
              :y2="s.y"
            />
            <polygon class="cr-radar-shape" :points="radar.shape" />
            <circle
              v-for="(d, i) in radar.dots"
              :key="`d${i}`"
              class="cr-radar-dot"
              :cx="d.x"
              :cy="d.y"
              r="3"
              :style="{ animationDelay: `calc(var(--duration-1) * var(--motion-scale) * ${i + 1})` }"
            />
            <text
              v-for="(l, i) in radar.labels"
              :key="`l${i}`"
              class="cr-radar-label"
              :x="l.x"
              :y="l.y"
            >
              {{ l.text }}
            </text>
          </svg>
        </div>
        <p v-else class="cr-note cr-empty">{{ t('achievements.career.taste.empty') }}</p>
      </div>
    </div>

    <div class="cr-grid3">
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">{{ t('achievements.career.taste.devTitle') }}</span>
          <span class="cr-head-hint">{{ t('achievements.career.taste.genreHint') }}</span>
        </div>
        <div v-for="d in developers" :key="d.name" class="cr-rank">
          <div class="cr-rank-top">
            <span class="cr-rank-name" :title="d.name">{{ d.name }}</span>
            <span class="cr-rank-meta">
              <span class="hl-num">{{ fmtHours(d.playtimeMin, t) }}</span>
              <span>{{ t('achievements.career.taste.games', { n: d.games }) }}</span>
            </span>
          </div>
          <div class="cr-bar">
            <i :style="{ width: `${d.pct}%`, background: PALETTE.violet }" />
          </div>
        </div>
        <p v-if="!developers.length" class="cr-note cr-empty">
          {{ t('achievements.career.taste.empty') }}
        </p>
      </div>

      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">{{ t('achievements.career.taste.pubTitle') }}</span>
          <span class="cr-head-hint">{{ t('achievements.career.taste.genreHint') }}</span>
        </div>
        <div v-for="d in publishers" :key="d.name" class="cr-rank">
          <div class="cr-rank-top">
            <span class="cr-rank-name" :title="d.name">{{ d.name }}</span>
            <span class="cr-rank-meta">
              <span class="hl-num">{{ fmtHours(d.playtimeMin, t) }}</span>
              <span>{{ t('achievements.career.taste.games', { n: d.games }) }}</span>
            </span>
          </div>
          <div class="cr-bar">
            <i :style="{ width: `${d.pct}%`, background: PALETTE.pink }" />
          </div>
        </div>
        <p v-if="!publishers.length" class="cr-note cr-empty">
          {{ t('achievements.career.taste.empty') }}
        </p>
      </div>

      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">{{ t('achievements.career.taste.seriesTitle') }}</span>
          <span class="cr-head-hint">{{ t('achievements.career.taste.genreHint') }}</span>
        </div>
        <div v-for="d in series" :key="d.name" class="cr-rank">
          <div class="cr-rank-top">
            <span class="cr-rank-name" :title="d.name">{{ d.name }}</span>
            <span class="cr-rank-meta">
              <span class="hl-num">{{ fmtHours(d.playtimeMin, t) }}</span>
              <span>{{ t('achievements.career.taste.games', { n: d.games }) }}</span>
            </span>
          </div>
          <div class="cr-bar">
            <i :style="{ width: `${d.pct}%`, background: PALETTE.teal }" />
          </div>
        </div>
        <p v-if="!series.length" class="cr-note cr-empty">
          {{ t('achievements.career.taste.empty') }}
        </p>
      </div>
    </div>

    <div class="cr-grid2">
      <!-- 年代构成 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="calendar" />
            {{ t('achievements.career.taste.decadeTitle') }}
          </span>
          <span class="cr-head-hint">{{ t('achievements.career.taste.decadeHint') }}</span>
        </div>
        <div class="cr-decade-bar">
          <i
            v-for="d in decades"
            :key="d.decade"
            :style="{ width: `${d.pct}%`, background: d.color }"
            :title="d.decade"
          />
        </div>
        <div class="cr-decade-legend">
          <span v-for="d in decades" :key="d.decade" class="cr-decade-item">
            <i class="cr-decade-dot" :style="{ background: d.color }" />
            {{ d.decade }}
            <b class="hl-num">{{ t('achievements.career.taste.games', { n: d.games }) }}</b>
            <span class="hl-num cr-decade-pct">
              {{ t('achievements.career.taste.share', { pct: d.pct }) }}
            </span>
          </span>
        </div>
      </div>

      <!-- 偏好标签 + 最老/最新 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="heart" />
            {{ t('achievements.career.taste.title') }}
          </span>
          <span class="cr-head-hint">{{ t('achievements.career.taste.hint') }}</span>
        </div>
        <div class="cr-kpi">
          <HlStat
            size="sm"
            :color="PALETTE.salmon"
            :style="{ borderColor: PALETTE.salmon + '30' }"
            :label="t('achievements.career.taste.chinese')"
            :value="String(tags.chinese)"
            :sub="t('achievements.career.taste.share', { pct: tags.chinesePct })"
          />
          <HlStat
            size="sm"
            :color="PALETTE.lime"
            :style="{ borderColor: PALETTE.lime + '30' }"
            :label="t('achievements.career.taste.fresh')"
            :value="String(tags.fresh)"
          />
          <HlStat
            size="sm"
            :color="PALETTE.amber"
            :style="{ borderColor: PALETTE.amber + '30' }"
            :label="t('achievements.career.taste.avgYear')"
            :value="String(tags.avgYear)"
          />
          <HlStat
            size="sm"
            :color="PALETTE.green"
            :style="{ borderColor: PALETTE.green + '30' }"
            :label="t('achievements.career.title.scope.avgPositive')"
            :value="t('achievements.career.taste.share', { pct: tags.positive })"
          />
        </div>
        <div class="cr-taste-edge">
          <article v-if="oldest" class="cr-edge-card">
            <div class="cr-edge-cover">
              <HlImg class="cr-edge-img" :src="oldest.headerImage" alt="" loading="lazy">
                <template #fallback><div class="cr-edge-fb">{{ oldest.name.slice(0, 1) }}</div></template>
              </HlImg>
            </div>
            <div class="cr-edge-body">
              <span class="cr-edge-label">{{ t('achievements.career.taste.oldest') }}</span>
              <span class="cr-edge-name" :title="oldest.name">{{ oldest.name }}</span>
              <span class="cr-edge-meta hl-num">
                {{ t('achievements.career.taste.release', { date: oldest.releaseDate }) }}
              </span>
            </div>
          </article>
          <article v-if="newest" class="cr-edge-card">
            <div class="cr-edge-cover">
              <HlImg class="cr-edge-img" :src="newest.headerImage" alt="" loading="lazy">
                <template #fallback><div class="cr-edge-fb">{{ newest.name.slice(0, 1) }}</div></template>
              </HlImg>
            </div>
            <div class="cr-edge-body">
              <span class="cr-edge-label">{{ t('achievements.career.taste.newest') }}</span>
              <span class="cr-edge-name" :title="newest.name">{{ newest.name }}</span>
              <span class="cr-edge-meta hl-num">
                {{ t('achievements.career.taste.release', { date: newest.releaseDate }) }}
              </span>
            </div>
          </article>
        </div>
        <p class="cr-note cr-taste-foot">{{ t('achievements.career.taste.decadeHint') }}</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.cr-pill--plat {
  border-color: var(--gild-2);
}

/* 雷达 */
.cr-radar-box {
  display: grid;
  place-items: center;
}

.cr-radar {
  width: 208px;
  height: 208px;
}

.cr-radar-ring {
  fill: none;
  stroke: var(--line-1);
  stroke-width: 1;
}

.cr-radar-spoke {
  stroke: var(--line-1);
  stroke-width: 1;
}

.cr-radar-shape {
  fill: var(--accent-fill);
  fill-opacity: 0.22;
  stroke: var(--accent);
  stroke-width: 1.6;
  animation: crRadarIn calc(var(--duration-5) * var(--motion-scale) * 2) var(--ease-out) backwards;
}

@keyframes crRadarIn {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
}

.cr-radar-dot {
  fill: var(--accent);
  animation: crRadarIn calc(var(--duration-4) * var(--motion-scale)) var(--ease-out) backwards;
}

.cr-radar-label {
  fill: var(--text-secondary);
  font-size: 9px;
  text-anchor: middle;
}

/* 年代构成 */
.cr-decade-bar {
  display: flex;
  height: 14px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface-track);
}

.cr-decade-bar i {
  display: block;
  height: 100%;
  transition: width calc(var(--duration-5) * var(--motion-scale)) var(--ease-out);
}

.cr-decade-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 8px;
}

.cr-decade-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  color: var(--text-secondary);
}

.cr-decade-dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}

.cr-decade-pct {
  color: var(--text-dim);
}

/* 最老 / 最新 */
.cr-taste-edge {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  margin-top: 10px;
}

.cr-edge-card {
  display: flex;
  gap: 8px;
  padding: 6px;
  border: 1px solid var(--line-1);
  border-radius: var(--radius-sm);
  background: var(--surface-inset-sm);
  min-width: 0;
}

.cr-edge-cover {
  flex: 0 0 auto;
  width: 62px;
  height: 30px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--surface-track);
}

.cr-edge-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.cr-edge-fb {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  font-size: 14px;
  font-weight: 700;
  color: var(--text-dim);
}

.cr-edge-body {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.cr-edge-label {
  font-size: 9px;
  color: var(--text-dim);
}

.cr-edge-name {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-edge-meta {
  font-size: 9.5px;
  color: var(--text-dim);
}

.cr-taste-foot {
  margin-top: 8px;
}
</style>