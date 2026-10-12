<script setup lang="ts">
/* 挑战中心：铜/银/金分档筛选 + 卡片栅格 + 领取动作（可领取才亮）。
   进度、状态、可领取判定全部消费后端字段；领取结果即时反馈（入账飘字 / 涨级横幅）。 */
import { computed, ref } from 'vue'

import type { ChallengeCategory, ChallengeItem, ChallengeStatus, ChallengeTier } from '@/api/client'
import { HlButton, HlSegmented, HlSkeleton, type HlSelectOption } from '@/components/ui'
import type { MessageKey } from '@/locales'
import { useI18n } from '@/locales'
import { vStagger } from '@/lib/stagger'
import { useTrophyHallStore } from '@/stores/trophyHall'

const { t } = useI18n()
const store = useTrophyHallStore()

const TIER_KEYS: Record<ChallengeTier, MessageKey> = {
  bronze: 'achievements.challenge.tier.bronze',
  silver: 'achievements.challenge.tier.silver',
  gold: 'achievements.challenge.tier.gold',
}

const CATEGORY_KEYS: Record<ChallengeCategory, MessageKey> = {
  milestone: 'achievements.challenge.category.milestone',
  cumulative: 'achievements.challenge.category.cumulative',
  streak: 'achievements.challenge.category.streak',
  collection: 'achievements.challenge.category.collection',
  platinum: 'achievements.challenge.category.platinum',
  comment: 'achievements.challenge.category.comment',
  social: 'achievements.challenge.category.social',
}

const STATUS_KEYS: Record<ChallengeStatus, MessageKey> = {
  active: 'achievements.challenge.status.active',
  completed: 'achievements.challenge.status.completed',
  claimed: 'achievements.challenge.status.claimed',
}

/* 条目名 / 描述按目录 id 现译（动态键，与事实通知同一取词路径） */
function itemName(c: ChallengeItem): MessageKey {
  return `achievements.challenge.item.${c.challengeId}.name` as MessageKey
}

function itemDesc(c: ChallengeItem): MessageKey {
  return `achievements.challenge.item.${c.challengeId}.desc` as MessageKey
}

type TierFilter = 'all' | ChallengeTier
const tierFilter = ref<TierFilter>('all')

const filterOptions = computed<HlSelectOption[]>(() => [
  { label: t('achievements.challenge.filter.all'), value: 'all' },
  { label: t('achievements.challenge.tier.bronze'), value: 'bronze' },
  { label: t('achievements.challenge.tier.silver'), value: 'silver' },
  { label: t('achievements.challenge.tier.gold'), value: 'gold' },
])

const payload = computed(() => store.challenges)
const visible = computed(() =>
  (payload.value?.challenges ?? []).filter(
    (c) => tierFilter.value === 'all' || c.tier === tierFilter.value,
  ),
)

function pctOf(c: ChallengeItem): number {
  if (c.target <= 0) return 0
  return Math.min(100, Math.round((c.progress / c.target) * 1000) / 10)
}

/* ── 领取：写后由 store 拉新账本面，这里只做即时反馈 ── */
const claiming = ref<string | null>(null)
const claimError = ref('')
const flash = ref<{ id: string; xp: number } | null>(null)
const leveledTo = ref(0)
let flashTimer = 0
let levelTimer = 0

async function claim(c: ChallengeItem): Promise<void> {
  if (claiming.value) return
  claiming.value = c.challengeId
  claimError.value = ''
  try {
    const res = await store.claim(c.challengeId)
    flash.value = { id: res.challengeId, xp: res.xpGranted }
    window.clearTimeout(flashTimer)
    flashTimer = window.setTimeout(() => {
      flash.value = null
    }, 1800)
    if (res.level.leveledUp) {
      leveledTo.value = res.level.current
      window.clearTimeout(levelTimer)
      levelTimer = window.setTimeout(() => {
        leveledTo.value = 0
      }, 6000)
    }
  } catch (e) {
    claimError.value = e instanceof Error ? e.message : String(e)
  } finally {
    claiming.value = null
  }
}

function fmt(n: number): string {
  return n.toLocaleString()
}
</script>

<template>
  <div>
    <HlSkeleton v-if="!payload && store.loading" variant="text" :count="2" :rows="3" />
    <p v-else-if="!payload && store.error" class="cr-note ch-error">{{ store.error }}</p>
    <div v-else-if="payload" class="cr-card ch-card">
      <div class="cr-head">
        <span class="cr-head-title">{{ t('achievements.section.challenges') }}</span>
        <span class="cr-head-hint">
          {{
            t('achievements.challenge.summary', {
              done: payload.summary.completed,
              total: payload.summary.total,
              claimable: payload.summary.claimable,
            })
          }}
        </span>
      </div>

      <div class="ch-toolbar">
        <HlSegmented v-model="tierFilter" :options="filterOptions" />
      </div>

      <p v-if="claimError" class="ch-error">{{ claimError }}</p>
      <p v-if="leveledTo" class="ch-levelup">
        {{ t('achievements.challenge.action.leveledUp', { n: leveledTo }) }}
      </p>

      <div v-if="visible.length" class="ch-grid hl-stagger" v-stagger>
        <div
          v-for="c in visible"
          :key="c.challengeId"
          class="ch-item"
          :class="[`is-${c.tier}`, { 'is-flash': flash?.id === c.challengeId }]"
        >
          <div class="ch-item-top">
            <span class="ch-cat">{{ t(CATEGORY_KEYS[c.category]) }}</span>
            <span class="ch-tier">{{ t(TIER_KEYS[c.tier]) }}</span>
          </div>
          <div class="ch-name" :title="t(itemName(c))">{{ t(itemName(c)) }}</div>
          <div class="ch-desc">{{ t(itemDesc(c)) }}</div>
          <div class="ch-progress">
            <div class="cr-bar"><i :style="{ width: pctOf(c) + '%' }" /></div>
            <span class="ch-progress-num hl-num">
              {{ t('achievements.challenge.progressOf', { progress: fmt(c.progress), target: fmt(c.target) }) }}
            </span>
          </div>
          <div class="ch-bottom">
            <span class="ch-reward hl-num">
              {{ t('achievements.challenge.action.reward', { n: c.rewardXp }) }}
            </span>
            <span v-if="c.status === 'claimed'" class="ch-state is-claimed">
              {{ t(STATUS_KEYS[c.status]) }}
            </span>
            <span v-else-if="!c.claimable" class="ch-state">{{ t(STATUS_KEYS[c.status]) }}</span>
            <HlButton
              v-else
              size="sm"
              variant="primary"
              :loading="claiming === c.challengeId"
              @click="claim(c)"
            >
              {{ t('achievements.challenge.action.claim') }}
            </HlButton>
          </div>
          <span v-if="flash?.id === c.challengeId" class="ch-grant hl-num">
            {{ t('achievements.challenge.action.granted', { n: flash.xp }) }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ch-error {
  font-size: 11.5px;
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
  border-radius: var(--radius-sm);
  padding: 7px 11px;
  margin: 8px 0 0;
}

.ch-levelup {
  font-size: 11.5px;
  font-weight: 700;
  color: var(--success);
  background: color-mix(in srgb, var(--success) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--success) 30%, transparent);
  border-radius: var(--radius-sm);
  padding: 6px 11px;
  margin: 8px 0 0;
}

.ch-toolbar { margin-top: 2px; }

.ch-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(228px, 1fr));
  gap: 8px;
  margin-top: 10px;
}

/* 档位色只落在卡侧 3px 条与档位字上（--ch-tier-color 由 is-* 档位类注入） */
.ch-item {
  position: relative;
  border: 1px solid var(--line-1);
  border-radius: var(--radius-sm);
  background: var(--surface-inset);
  padding: 10px 12px;
  overflow: hidden;
  transition: border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}

.ch-item::before {
  content: '';
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: var(--ch-tier-color);
}

.ch-item.is-bronze { --ch-tier-color: var(--medal-bronze-2); }
.ch-item.is-silver { --ch-tier-color: var(--medal-silver-2); }
.ch-item.is-gold { --ch-tier-color: var(--medal-gold-2); }

@keyframes chFlash {
  0% { border-color: var(--accent-fill); transform: scale(1.015); }
  100% { border-color: var(--line-1); transform: scale(1); }
}

.ch-item.is-flash {
  animation: chFlash calc(var(--duration-5) * var(--motion-scale) * 2) var(--ease-out);
}

.ch-item-top { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
.ch-cat {
  font-size: 9.5px;
  color: var(--text-dim);
  border: 1px solid var(--line-1);
  border-radius: 999px;
  padding: 1px 7px;
  white-space: nowrap;
}
.ch-tier { font-size: 9.5px; font-weight: 800; letter-spacing: 0.06em; color: var(--ch-tier-color); }

.ch-name {
  font-size: 12.5px;
  font-weight: 700;
  color: var(--text-primary);
  margin-top: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ch-desc { font-size: 10px; line-height: 1.55; color: var(--text-dim); margin-top: 2px; min-height: 31px; }

.ch-progress { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.ch-progress .cr-bar { flex: 1; }
.ch-progress-num { flex-shrink: 0; font-size: 10px; color: var(--text-secondary); }

.ch-bottom { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 9px; }
.ch-reward { font-size: 11px; font-weight: 800; color: var(--accent); }

.ch-state {
  font-size: 9.5px;
  font-weight: 700;
  color: var(--text-dim);
  border: 1px solid var(--line-1);
  border-radius: 999px;
  padding: 1px 8px;
  white-space: nowrap;
}
.ch-state.is-claimed {
  color: var(--success);
  border-color: color-mix(in srgb, var(--success) 35%, transparent);
}

@keyframes chRise {
  0% { opacity: 0; transform: translateY(4px); }
  15% { opacity: 1; }
  80% { opacity: 1; }
  100% { opacity: 0; transform: translateY(-10px); }
}

.ch-grant {
  position: absolute;
  right: 10px;
  top: 8px;
  font-size: 11px;
  font-weight: 800;
  color: var(--success);
  pointer-events: none;
  animation: chRise 1.8s var(--ease-out) both;
}

@media (prefers-reduced-motion: reduce) {
  .ch-item.is-flash, .ch-grant { animation: none; }
}
</style>
