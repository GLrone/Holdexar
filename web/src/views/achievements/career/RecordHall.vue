<script setup lang="ts">
/* 纪录殿堂：生涯八项极值 + 白金用时读数 + 里程碑时间轴。
 *
 * 每条纪录都能追到原始记录（哪一款游戏、哪一天、多少枚），不做加权分、
 * 不做估算——这类二次评分需要人工标注，本地数据没有，硬造出来就是编数。
 * 所以这里的「纪录」一律是可复算的极值。
 *
 * 口径提醒：单游戏「通关跨度」要求至少 5 条解锁且时长过 1 小时（后端
 * SPAN_MIN_* 门槛）——否则「3 枚成就挤在 5 分钟里」会被算成最快通关，
 * 那是个假纪录。 */
import { computed } from 'vue'

import type { CareerPayload } from '@/api/client'
import { HlIcon, HlImg, HlStat } from '@/components/ui'
import TrophyMedal from '@/components/business/TrophyMedal.vue'
import type { IconName } from '@/components/ui/icons'
import { useI18n } from '@/locales'
import type { MessageKey } from '@/locales'
import { formatMetric } from '@/lib/careerTitles'
import { PALETTE } from '@/lib/familyColors'
import { vStagger } from '@/lib/stagger'
import { fmtDate, fmtDuration, fmtHours, sheenStyle } from './careerUtil'

const props = defineProps<{ career: CareerPayload }>()

const { t } = useI18n()

interface RecGame {
  appid: number
  name: string
  headerImage: string
}

interface RecRow {
  id: string
  icon: IconName
  labelKey: MessageKey
  hintKey: MessageKey
  value: string
  sub: string
  game: RecGame | null
  color: string
  /** 珍贵瞬间：挂扫光 + 呼吸微光（整页只有两条，多了就没有对比度） */
  precious?: boolean
  seed: number
}

const rec = computed(() => props.career.records)
const trophy = computed(() => props.career.trophy)

function brief(g: { appid: number; name: string; headerImage: string } | null): RecGame | null {
  return g ? { appid: g.appid, name: g.name, headerImage: g.headerImage } : null
}

const records = computed<RecRow[]>(() => {
  const r = rec.value
  const fast = r.fastestComplete
  const slow = r.slowestComplete
  const most = r.mostUnlocksGame
  const big = r.biggestPlatinum

  return [
    {
      id: 'fastest',
      icon: 'zap',
      labelKey: 'achievements.career.rec.fastest',
      hintKey: 'achievements.career.rec.fastestHint',
      value: fast ? fmtDuration(fast.spanMin, t) : t('achievements.career.rec.valueEmpty'),
      sub: fast ? t('achievements.career.rec.unlocks', { n: fast.unlocks }) : '',
      game: brief(fast),
      color: PALETTE.lime,
      seed: fast?.appid ?? 1,
    },
    {
      id: 'slowest',
      icon: 'calendar',
      labelKey: 'achievements.career.rec.slowest',
      hintKey: 'achievements.career.rec.slowestHint',
      value: slow ? fmtDuration(slow.spanMin, t) : t('achievements.career.rec.valueEmpty'),
      sub: slow ? t('achievements.career.rec.unlocks', { n: slow.unlocks }) : '',
      game: brief(slow),
      color: PALETTE.slate,
      seed: slow?.appid ?? 2,
    },
    {
      id: 'marathon',
      icon: 'play',
      labelKey: 'achievements.career.rec.marathon',
      hintKey: 'achievements.career.rec.marathonHint',
      value: r.marathonDay ? String(r.marathonDay.count) : t('achievements.career.rec.valueEmpty'),
      sub: r.marathonDay ? r.marathonDay.date : '',
      game: null,
      color: PALETTE.orange,
      precious: true,
      seed: 3,
    },
    {
      id: 'busiest',
      icon: 'dashboard',
      labelKey: 'achievements.career.rec.busiestHour',
      hintKey: 'achievements.career.rec.busiestHourHint',
      value: `${r.busiestHour}:00`,
      sub: '',
      game: null,
      color: PALETTE.sky,
      seed: 4,
    },
    {
      id: 'most',
      icon: 'trophy',
      labelKey: 'achievements.career.rec.mostUnlocks',
      hintKey: 'achievements.career.rec.mostUnlocksHint',
      value: most ? t('achievements.career.rec.unlocks', { n: most.unlocked }) : t('achievements.career.rec.valueEmpty'),
      sub: '',
      game: brief(most),
      color: PALETTE.violet,
      seed: most?.appid ?? 5,
    },
    {
      id: 'biggest',
      icon: 'star',
      labelKey: 'achievements.career.rec.biggestPlat',
      hintKey: 'achievements.career.rec.biggestPlatHint',
      value: big ? t('achievements.career.rec.unlocks', { n: big.total }) : t('achievements.career.rec.valueEmpty'),
      sub: '',
      game: brief(big),
      color: PALETTE.gold,
      precious: true,
      seed: big?.appid ?? 6,
    },
    {
      id: 'efficiency',
      icon: 'chart',
      labelKey: 'achievements.career.rec.efficiency',
      hintKey: 'achievements.career.rec.efficiencyHint',
      value: formatMetric(trophy.value.perHour, 'perHour', t),
      sub: '',
      game: null,
      color: PALETTE.teal,
      seed: 7,
    },
    {
      id: 'rarity',
      icon: 'target',
      labelKey: 'achievements.career.rec.avgRarity',
      hintKey: 'achievements.career.rec.avgRarityHint',
      value: formatMetric(trophy.value.avgRarity, 'percent', t),
      sub: '',
      game: null,
      color: PALETTE.pink,
      seed: 8,
    },
  ]
})

/* ── 白金用时读数 ── */
const plat = computed(() => props.career.platinum)

/* ── 里程碑时间轴 ── */
const milestones = computed(() =>
  props.career.milestones.map((m) => ({
    ...m,
    rare: m.kind === 'rarest',
    date: fmtDate(m.at),
    globalText:
      typeof m.globalPercent === 'number'
        ? t('achievements.career.vault.quotePct', { pct: m.globalPercent })
        : '',
  })),
)
</script>

<template>
  <section data-section="achievements.section.records" class="cr-stack">
    <div class="cr-card">
      <div class="cr-head">
        <span class="cr-head-title">
          <TrophyMedal tier="gold" :size="20" />
          {{ t('achievements.career.rec.title') }}
        </span>
        <span class="cr-head-hint">{{ t('achievements.career.rec.hint') }}</span>
      </div>

      <div v-if="records.length" class="cr-rec-grid hl-stagger" v-stagger>
        <article
          v-for="row in records"
          :key="row.id"
          class="cr-rec"
          :class="{ 'cr-sheen': row.precious, 'cr-gilded': row.precious, 'cr-glow': row.precious }"
          :style="{ '--cr-tone': row.color, ...sheenStyle(row.seed) }"
        >
          <div class="cr-rec-top">
            <span class="cr-rec-icon"><HlIcon :name="row.icon" /></span>
            <span class="cr-rec-label">{{ t(row.labelKey) }}</span>
          </div>
          <div class="cr-rec-value hl-num">{{ row.value }}</div>
          <div class="cr-rec-sub">
            <span v-if="row.sub" class="hl-num">{{ row.sub }}</span>
            <span class="cr-rec-hint">{{ t(row.hintKey) }}</span>
          </div>
          <div v-if="row.game" class="cr-rec-game">
            <HlImg class="cr-rec-cover" :src="row.game.headerImage" alt="" loading="lazy">
              <template #fallback><div class="cr-rec-fb">{{ row.game.name.slice(0, 1) }}</div></template>
            </HlImg>
            <span class="cr-rec-name" :title="row.game.name">{{ row.game.name }}</span>
          </div>
        </article>
      </div>
      <p v-else class="cr-note cr-empty">{{ t('achievements.career.rec.empty') }}</p>
    </div>

    <div class="cr-grid2">
      <!-- 白金用时 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="star" />
            {{ t('achievements.career.rec.platViewTitle') }}
          </span>
          <span class="cr-head-hint">
            {{ t('achievements.career.ov.platinumSub') }}
          </span>
        </div>
        <div class="cr-kpi">
          <HlStat
            size="sm"
            :color="PALETTE.gold"
            :style="{ borderColor: PALETTE.gold + '30' }"
            :label="t('achievements.career.rec.platCount')"
            :value="String(plat.count)"
          />
          <HlStat
            size="sm"
            :color="PALETTE.lime"
            :style="{ borderColor: PALETTE.lime + '30' }"
            :label="t('achievements.career.rec.platFastest')"
            :value="plat.fastest ? fmtHours(plat.fastest.playtimeMin, t) : t('achievements.career.rec.valueEmpty')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.slate"
            :style="{ borderColor: PALETTE.slate + '30' }"
            :label="t('achievements.career.rec.platSlowest')"
            :value="plat.slowest ? fmtHours(plat.slowest.playtimeMin, t) : t('achievements.career.rec.valueEmpty')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.blue"
            :style="{ borderColor: PALETTE.blue + '30' }"
            :label="t('achievements.career.rec.platAvg')"
            :value="fmtHours(plat.avgMin, t)"
            :sub="`${t('achievements.career.rec.platMedian')} ${fmtHours(plat.medianMin, t)}`"
          />
        </div>
        <div class="cr-plat-pair">
          <div v-if="plat.fastest" class="cr-plat-card">
            <span class="cr-pill cr-pill--fast">{{ t('achievements.career.rec.platFastest') }}</span>
            <span class="cr-plat-name" :title="plat.fastest.name">{{ plat.fastest.name }}</span>
            <span class="cr-plat-meta hl-num">{{ fmtHours(plat.fastest.playtimeMin, t) }}</span>
          </div>
          <div v-if="plat.slowest" class="cr-plat-card">
            <span class="cr-pill">{{ t('achievements.career.rec.platSlowest') }}</span>
            <span class="cr-plat-name" :title="plat.slowest.name">{{ plat.slowest.name }}</span>
            <span class="cr-plat-meta hl-num">{{ fmtHours(plat.slowest.playtimeMin, t) }}</span>
          </div>
        </div>
      </div>

      <!-- 里程碑时间轴 -->
      <div class="cr-card">
        <div class="cr-head">
          <span class="cr-head-title">
            <HlIcon name="target" />
            {{ t('achievements.career.rec.milestoneTitle') }}
          </span>
          <span class="cr-head-hint">{{ t('achievements.career.rec.milestoneHint') }}</span>
        </div>
        <ol v-if="milestones.length" class="cr-ms cr-scroll hl-stagger" v-stagger>
          <li
            v-for="m in milestones"
            :key="`${m.kind}-${m.index}-${m.at}`"
            class="cr-ms-item"
            :class="{ 'is-rare': m.rare }"
          >
            <span class="cr-ms-dot" :class="{ 'cr-glow': m.rare }">
              <HlIcon :name="m.rare ? 'star' : 'trophy'" />
            </span>
            <div class="cr-ms-body">
              <div class="cr-ms-head">
                <span class="cr-ms-label">
                  {{
                    m.rare
                      ? t('achievements.career.rec.milestoneRarest')
                      : t('achievements.career.rec.milestoneCount', { n: m.index })
                  }}
                </span>
                <span class="cr-ms-date hl-num">{{ m.date }}</span>
              </div>
              <div class="cr-ms-ach" :title="m.name">{{ m.name }}</div>
              <div class="cr-ms-game">
                <span :title="m.gameName">{{ m.gameName }}</span>
                <span v-if="m.globalText" class="cr-pill cr-pill--rare">{{ m.globalText }}</span>
              </div>
            </div>
            <HlImg v-if="m.headerImage" class="cr-ms-cover" :src="m.headerImage" alt="" loading="lazy">
              <template #fallback><div class="cr-ms-fb" /></template>
            </HlImg>
          </li>
        </ol>
        <p v-else class="cr-note cr-empty">
          {{ t('achievements.career.rec.milestoneEmpty') }}
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.cr-rec-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(184px, 1fr));
  gap: 8px;
}

.cr-rec {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 9px 10px;
  border: 1px solid var(--line-1);
  border-top: 3px solid var(--cr-tone);
  border-radius: var(--radius-sm);
  background: var(--surface-inset-sm);
  transition: transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.cr-rec:hover {
  transform: translateY(-2px);
  border-color: var(--cr-tone);
}

.cr-rec-top {
  display: flex;
  align-items: center;
  gap: 5px;
}

.cr-rec-icon {
  display: inline-flex;
  color: var(--cr-tone);
}

.cr-rec-label {
  font-size: 10.5px;
  font-weight: 700;
  color: var(--text-secondary);
}

.cr-rec-value {
  font-size: 20px;
  font-weight: 800;
  line-height: 1.1;
  color: var(--text-primary);
}

.cr-rec-sub {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  font-size: 9.5px;
  color: var(--text-dim);
}

.cr-rec-hint {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-rec-game {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  min-width: 0;
}

.cr-rec-cover {
  flex: 0 0 auto;
  width: 44px;
  height: 21px;
  border-radius: 3px;
  object-fit: cover;
  display: block;
  background: var(--surface-track);
}

.cr-rec-fb {
  width: 44px;
  height: 21px;
  border-radius: 3px;
  display: grid;
  place-items: center;
  font-size: 10px;
  font-weight: 700;
  color: var(--text-dim);
  background: var(--surface-track);
}

.cr-rec-name {
  font-size: 10px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 白金用时：最快 / 最慢两张对照卡 */
.cr-plat-pair {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  margin-top: 10px;
}

.cr-plat-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 7px 8px;
  border: 1px solid var(--line-1);
  border-radius: var(--radius-sm);
  background: var(--surface-inset-sm);
  min-width: 0;
}

.cr-pill--fast {
  border-color: var(--gild-2);
}

.cr-plat-name {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-plat-meta {
  font-size: 9.5px;
  color: var(--text-dim);
}

/* 里程碑时间轴：左列圆点 + 贯穿竖线，右侧信息，末列封面 */
.cr-ms {
  list-style: none;
  margin: 0;
  padding: 0;
}

.cr-ms-item {
  position: relative;
  display: grid;
  grid-template-columns: 20px 1fr 78px;
  gap: 8px;
  padding-bottom: 9px;
  align-items: start;
}

.cr-ms-item::before {
  content: '';
  position: absolute;
  left: 9px;
  top: 20px;
  bottom: 0;
  width: 1px;
  background: var(--line-1);
}

.cr-ms-item:last-child::before {
  display: none;
}

.cr-ms-dot {
  position: relative;
  z-index: 1;
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: 1px solid var(--line-1);
  background: var(--surface-inset);
  color: var(--text-secondary);
}

.cr-ms-item.is-rare .cr-ms-dot {
  border-color: var(--gild-2);
  color: var(--gild-2);
}

.cr-ms-body {
  min-width: 0;
}

.cr-ms-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
}

.cr-ms-label {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-primary);
}

.cr-ms-item.is-rare .cr-ms-label {
  color: var(--gild-2);
}

.cr-ms-date {
  font-size: 9.5px;
  color: var(--text-dim);
}

.cr-ms-ach {
  font-size: 10.5px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-ms-game {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 9.5px;
  color: var(--text-dim);
  min-width: 0;
}

.cr-ms-game > span:first-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cr-pill--rare {
  border-color: var(--gild-2);
  color: var(--gild-2);
  flex: 0 0 auto;
}

.cr-ms-cover {
  width: 78px;
  height: 34px;
  border-radius: 4px;
  object-fit: cover;
  display: block;
  background: var(--surface-track);
}

.cr-ms-fb {
  width: 78px;
  height: 34px;
  border-radius: 4px;
  background: var(--surface-track);
}
</style>