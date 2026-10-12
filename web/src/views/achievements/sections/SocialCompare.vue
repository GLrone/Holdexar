<script setup lang="ts">
/* 账号对比：等级 / 白金 / 时长 / 挑战 / 稀有度分布同屏，条长即位次（不排第一名次标签）。
   数值全部出自后端 compare 聚合，本组件零本地推算；稀有度档序与颜色
   沿用成就页同一套（achievements.rarity.* 词条复用）。 */
import { computed, onMounted, ref } from 'vue'

import type { CompareAccount, LevelTier, RarityTier } from '@/api/client'
import { HlAvatar, HlChip, HlEmpty, HlIcon, HlSkeleton } from '@/components/ui'
import type { HlSelectOption } from '@/components/ui'
import type { MessageKey } from '@/locales'
import { useI18n } from '@/locales'
import { MEMBER_COLORS, PALETTE } from '@/lib/familyColors'
import { useAchievementsStore } from '@/stores/achievements'
import { useTrophyHallStore } from '@/stores/trophyHall'
import { fmtHours } from '../career/careerUtil'

const COMPARE_MAX = 6

const { t } = useI18n()
const hall = useTrophyHallStore()
const achv = useAchievementsStore()

const comparing = ref(false)

onMounted(() => {
  if (!achv.accounts.length) void achv.loadAccounts()
  if (!hall.compare) void pick()
})

/** 拉对比载荷：缺省沿用上次勾选（空 = 后端取可切换账号前 4 个） */
async function pick(steamids?: string[]): Promise<void> {
  comparing.value = true
  try {
    await hall.loadCompare(steamids)
  } finally {
    comparing.value = false
  }
}

/** 勾选/取消一个账号（到顶后忽略新勾选，提示文案已交代上限） */
function toggle(steamid: string): void {
  const next = hall.compareSteamids.includes(steamid)
    ? hall.compareSteamids.filter((s) => s !== steamid)
    : hall.compareSteamids.length >= COMPARE_MAX
      ? hall.compareSteamids
      : [...hall.compareSteamids, steamid]
  void pick(next)
}

/** 行序 = 等级位次（并列看总经验），条长对比一眼可读 */
const rows = computed<CompareAccount[]>(() => {
  const list = hall.compare?.accounts ?? []
  return [...list].sort((a, b) => b.level - a.level || b.totalXp - a.totalXp)
})

const pickable = computed(() => achv.accounts)

const picked = (steamid: string): boolean => hall.compareSteamids.includes(steamid)

/* 四列数值指标：条宽按列内最大值归一，值文案走词典（时长转小时） */
interface Metric {
  id: string
  key: MessageKey
  of: (a: CompareAccount) => number
  text: (a: CompareAccount) => string
}

const METRICS: Metric[] = [
  {
    id: 'level',
    key: 'achievements.social.compare.metric.level',
    of: (a) => a.level,
    text: (a) => String(a.level),
  },
  {
    id: 'platinum',
    key: 'achievements.social.compare.metric.platinum',
    of: (a) => a.platinumCount,
    text: (a) => String(a.platinumCount),
  },
  {
    id: 'playtime',
    key: 'achievements.social.compare.metric.playtime',
    of: (a) => a.playtimeMinutes,
    text: (a) => fmtHours(a.playtimeMinutes, t),
  },
  {
    id: 'challenges',
    key: 'achievements.social.compare.metric.challenges',
    of: (a) => a.challengesCompleted,
    text: (a) => String(a.challengesCompleted),
  },
]

const barPct = (m: Metric, a: CompareAccount): number => {
  const max = Math.max(0, ...rows.value.map((x) => m.of(x)))
  return max > 0 ? Math.max(2, Math.round((m.of(a) / max) * 100)) : 0
}

/* 成员身份色：同一账号在任何一次对比里都是同一颜色（按 steamid 散列取槽） */
function memberTone(steamid: string): string {
  let h = 7
  for (const ch of steamid) h = (h * 31 + ch.charCodeAt(0)) % 997
  return MEMBER_COLORS[h % MEMBER_COLORS.length]!
}

/* 同底色胶囊的家配方：具名色 14% 透底 + 同色文字，双主题都可读 */
function toneBg(color: string): string {
  return `color-mix(in srgb, ${color} 14%, transparent)`
}

/* 段位色（与奖杯四档视觉同向；legend 归金系） */
const TIER_COLOR: Record<LevelTier, string> = {
  bronze: PALETTE.orange,
  silver: PALETTE.slate,
  gold: PALETTE.gold,
  platinum: PALETTE.teal,
  legend: PALETTE.violet,
}

const TIER_KEY: Record<LevelTier, MessageKey> = {
  bronze: 'achievements.level.tier.bronze',
  silver: 'achievements.level.tier.silver',
  gold: 'achievements.level.tier.gold',
  platinum: 'achievements.level.tier.platinum',
  legend: 'achievements.level.tier.legend',
}

/* 稀有度堆叠：固定档序补零成段，段宽按占比；档名复用既有词条 */
const RARITY_ORDER = ['ultra', 'very_rare', 'rare', 'uncommon', 'common'] as const

const RARITY_COLOR: Record<(typeof RARITY_ORDER)[number], string> = {
  ultra: PALETTE.gold,
  very_rare: PALETTE.violet,
  rare: PALETTE.blue,
  uncommon: PALETTE.teal,
  common: PALETTE.slate,
}

const RARITY_KEY: Record<(typeof RARITY_ORDER)[number], MessageKey> = {
  ultra: 'achievements.rarity.ultra',
  very_rare: 'achievements.rarity.very_rare',
  rare: 'achievements.rarity.rare',
  uncommon: 'achievements.rarity.uncommon',
  common: 'achievements.rarity.common',
}

interface RaritySeg {
  tier: (typeof RARITY_ORDER)[number]
  pct: number
  title: string
}

function segments(a: CompareAccount): RaritySeg[] {
  const countOf = (tier: (typeof RARITY_ORDER)[number]): number =>
    a.rarityBuckets.find((b) => b.tier === tier)?.count ?? 0
  const total = RARITY_ORDER.reduce((sum, tier) => sum + countOf(tier), 0)
  if (total <= 0) return []
  return RARITY_ORDER.map((tier) => ({
    tier,
    pct: Math.round((countOf(tier) / total) * 100),
    title: `${t(RARITY_KEY[tier])} · ${countOf(tier)}`,
  })).filter((seg) => seg.pct > 0)
}

const rarityLegend = computed(() =>
  RARITY_ORDER.map((tier) => ({ tier, label: t(RARITY_KEY[tier]) })),
)
</script>

<template>
  <div class="cr-card so-compare">
    <div class="cr-head">
      <span class="cr-head-title">
        <HlIcon name="user" />
        {{ t('achievements.social.compare.title') }}
      </span>
      <span class="cr-head-hint">{{ t('achievements.social.compare.selectHint') }}</span>
    </div>

    <div v-if="pickable.length" class="so-pick" :class="{ 'is-busy': comparing }">
      <HlChip
        v-for="a in pickable"
        :key="a.steamid"
        shape="pill"
        :on="picked(a.steamid)"
        @click="toggle(a.steamid)"
      >
        {{ a.personaName }}<span v-if="a.relation === 'family'" class="so-pick-rel">
          {{ t('achievements.account.family') }}
        </span>
      </HlChip>
    </div>

    <HlSkeleton v-if="comparing && !rows.length" variant="text" :count="3" :rows="1" />
    <HlEmpty
      v-else-if="!rows.length"
      size="sm"
      icon=""
      :text="t('achievements.social.compare.empty')"
    />
    <template v-else>
      <div class="so-grid">
        <span class="so-grid-head" aria-hidden="true"></span>
        <span v-for="m in METRICS" :key="m.id" class="so-grid-head" :class="`so-m-${m.id}`">
          {{ t(m.key) }}
        </span>
        <span class="so-grid-head">{{ t('achievements.social.compare.metric.rarity') }}</span>

        <template v-for="a in rows" :key="a.steamid">
          <div class="so-who">
            <HlAvatar :src="a.avatarUrl" :name="a.personaName" size="sm" />
            <div class="so-who-main">
              <span class="so-who-name" :title="a.personaName">{{ a.personaName }}</span>
              <span class="so-who-sub hl-num">
                {{ t('achievements.social.compare.metric.unlocked') }} {{ a.unlockedCount }}
              </span>
            </div>
            <span
              class="so-tier"
              :style="{ color: TIER_COLOR[a.tier], background: toneBg(TIER_COLOR[a.tier]) }"
            >
              {{ t(TIER_KEY[a.tier]) }}
            </span>
          </div>
          <div v-for="m in METRICS" :key="m.id" class="so-cell" :class="`so-m-${m.id}`">
            <div class="so-bar">
              <i :style="{ width: `${barPct(m, a)}%`, background: memberTone(a.steamid) }" />
            </div>
            <span class="so-val hl-num">{{ m.text(a) }}</span>
          </div>
          <div class="so-cell so-rarity">
            <div class="so-stack">
              <i
                v-for="seg in segments(a)"
                :key="seg.tier"
                :style="{ width: `${seg.pct}%`, background: RARITY_COLOR[seg.tier] }"
                :title="seg.title"
              />
              <span v-if="!segments(a).length" class="so-stack-none">—</span>
            </div>
          </div>
        </template>
      </div>

      <div class="so-legend">
        <span v-for="seg in rarityLegend" :key="seg.tier" class="so-legend-item">
          <i :style="{ background: RARITY_COLOR[seg.tier] }" />
          {{ seg.label }}
        </span>
      </div>
    </template>
  </div>
</template>

<style src="../career/career-shared.css"></style>

<style scoped>
.so-pick {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
  transition: opacity calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.so-pick.is-busy {
  opacity: 0.6;
}

.so-pick-rel {
  font-size: 9px;
  opacity: 0.72;
  margin-left: 3px;
}

.so-grid {
  display: grid;
  grid-template-columns: minmax(148px, 1.35fr) repeat(4, 1fr) minmax(132px, 1.5fr);
  gap: 8px 10px;
  align-items: center;
}

.so-grid-head {
  font-size: 10px;
  font-weight: 700;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.so-who {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.so-who-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.so-who-name {
  font-size: 12px;
  font-weight: 700;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.so-who-sub {
  font-size: 9.5px;
  color: var(--text-dim);
}

.so-tier {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  font-size: 9px;
  font-weight: 700;
  border-radius: 4px;
  padding: 1px 6px;
  white-space: nowrap;
}

.so-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.so-bar {
  flex: 1;
  height: 5px;
  border-radius: 3px;
  background: var(--surface-track);
  overflow: hidden;
}

.so-bar i {
  display: block;
  height: 100%;
  border-radius: 3px;
  transition: width calc(var(--duration-5) * var(--motion-scale)) var(--ease-out);
}

.so-val {
  flex-shrink: 0;
  min-width: 34px;
  text-align: right;
  font-size: 10.5px;
  font-weight: 700;
  color: var(--text-secondary);
}

.so-stack {
  flex: 1;
  display: flex;
  height: 8px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--surface-track);
}

.so-stack i {
  display: block;
  height: 100%;
  min-width: 2px;
}

.so-stack-none {
  flex: 1;
  text-align: center;
  font-size: 9px;
  color: var(--text-dim);
  line-height: 8px;
}

.so-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin-top: 10px;
}

.so-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  color: var(--text-secondary);
}

.so-legend-item i {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}

/* 窄屏收起时长与挑战两列，保留等级 / 白金 / 稀有度三组读数 */
@media (max-width: 1100px) {
  .so-grid {
    grid-template-columns: minmax(128px, 1.3fr) repeat(2, 1fr) minmax(120px, 1.4fr);
  }

  .so-m-playtime,
  .so-m-challenges {
    display: none;
  }
}
</style>
