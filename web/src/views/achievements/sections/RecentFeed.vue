<script setup lang="ts">
/* 解锁动态：最近解锁竖排时间线（图标 + 名称 + 稀有度档 chip + 相对时间）。
   档名复用 achievements.rarity.* 词条；点击行抛 open-game 给宿主开游戏抽屉。 */
import { computed, onMounted } from 'vue'

import type { FeedItem, RarityTier } from '@/api/client'
import { HlEmpty, HlIcon, HlImg, HlSkeleton } from '@/components/ui'
import type { MessageKey } from '@/locales'
import { useI18n } from '@/locales'
import { PALETTE } from '@/lib/familyColors'
import TrophyMedal from '@/components/business/TrophyMedal.vue'
import { useTrophyHallStore } from '@/stores/trophyHall'

const emit = defineEmits<{ 'open-game': [appid: number] }>()

const { t } = useI18n()
const hall = useTrophyHallStore()

onMounted(() => {
  void hall.load()
})

const items = computed<FeedItem[]>(() => hall.feed)

/* 档位 → 颜色 + 词条 key（模块级只存 key，渲染期 t()） */
const RARITY_META: Record<RarityTier, { color: string; key: MessageKey }> = {
  ultra: { color: PALETTE.gold, key: 'achievements.rarity.ultra' },
  very_rare: { color: PALETTE.violet, key: 'achievements.rarity.very_rare' },
  rare: { color: PALETTE.blue, key: 'achievements.rarity.rare' },
  uncommon: { color: PALETTE.teal, key: 'achievements.rarity.uncommon' },
  common: { color: PALETTE.slate, key: 'achievements.rarity.common' },
  unknown: { color: PALETTE.slate, key: 'achievements.rarity.unknown' },
}

/* 头三档给奖杯立绘（与成就页稀有度徽章同规）；常见/少见不给杯 */
const RARITY_TROPHY: Partial<Record<RarityTier, 'gold' | 'silver' | 'bronze'>> = {
  ultra: 'gold',
  very_rare: 'silver',
  rare: 'bronze',
}

function rarityText(it: FeedItem): string {
  const label = t(RARITY_META[it.rarity].key)
  return it.globalPercent == null
    ? label
    : `${label} · ${it.globalPercent.toFixed(1)}%`
}

function rarityTitle(it: FeedItem): string {
  return it.globalPercent == null
    ? ''
    : t('achievements.drawer.globalPct', { pct: it.globalPercent.toFixed(1) })
}

/** 解锁时刻 → 相对时间；超出月级落回本地日期 */
function relTime(ts: number): string {
  if (!(ts > 0)) return ''
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return t('achievements.social.feed.justNow')
  if (diff < 3600) return t('achievements.social.feed.minutesAgo', { n: Math.floor(diff / 60) })
  if (diff < 86400) return t('achievements.social.feed.hoursAgo', { n: Math.floor(diff / 3600) })
  if (diff < 86400 * 30) return t('achievements.social.feed.daysAgo', { n: Math.floor(diff / 86400) })
  return new Date(ts * 1000).toLocaleDateString()
}
</script>

<template>
  <div class="cr-card so-feed">
    <div class="cr-head">
      <span class="cr-head-title">
        <HlIcon name="zap" />
        {{ t('achievements.social.feed.title') }}
      </span>
      <span class="cr-head-hint">{{ t('achievements.recent.hint') }}</span>
    </div>

    <HlSkeleton v-if="hall.loading && !items.length" variant="text" :count="5" :rows="1" />
    <HlEmpty v-else-if="!items.length" size="sm" icon="" :text="t('achievements.social.feed.empty')" />
    <div v-else class="so-feed-list hl-stagger">
      <button
        v-for="it in items"
        :key="`${it.appid}-${it.imageName}`"
        type="button"
        class="so-feed-row"
        @click="emit('open-game', it.appid)"
      >
        <div class="so-feed-icon">
          <HlImg :src="it.iconUrl" class="so-feed-img" alt="" loading="lazy">
            <template #fallback>
              <div class="so-feed-fb"><TrophyMedal tier="platinum" :size="24" /></div>
            </template>
          </HlImg>
        </div>
        <div class="so-feed-main">
          <span class="so-feed-name" :title="it.name">{{ it.name }}</span>
          <span class="so-feed-game" :title="it.gameName">{{ it.gameName }}</span>
        </div>
        <div class="so-feed-side">
          <span
            class="so-feed-rarity"
            :style="{
              color: RARITY_META[it.rarity].color,
              background: `color-mix(in srgb, ${RARITY_META[it.rarity].color} 12%, transparent)`,
            }"
            :title="rarityTitle(it)"
          >
            <TrophyMedal v-if="RARITY_TROPHY[it.rarity]" :tier="RARITY_TROPHY[it.rarity]" :size="13" />
            {{ rarityText(it) }}
          </span>
          <span class="so-feed-time hl-num">{{ relTime(it.unlockTime) }}</span>
        </div>
      </button>
    </div>
  </div>
</template>

<style src="../career/career-shared.css"></style>

<style scoped>
.so-feed-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  max-height: 320px;
  overflow-y: auto;
  overflow-x: hidden;
}

.so-feed-row {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 7px;
  border: 1px solid var(--line-1);
  border-radius: var(--radius-sm);
  background: var(--surface-inset);
  cursor: pointer;
  min-width: 0;
  text-align: left;
  transition: border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.so-feed-row:hover {
  border-color: var(--accent-a45);
  transform: translateY(-1px);
}

.so-feed-icon {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  border-radius: 5px;
  overflow: hidden;
  background: var(--surface-track);
}

.so-feed-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.so-feed-fb {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
}

.so-feed-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.so-feed-name {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.so-feed-game {
  font-size: 9.5px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.so-feed-side {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 3px;
}

.so-feed-rarity {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 9.5px;
  font-weight: 700;
  line-height: 1;
  border-radius: 4px;
  padding: 2px 6px;
  white-space: nowrap;
}

.so-feed-time {
  font-size: 9.5px;
  color: var(--text-dim);
}
</style>
