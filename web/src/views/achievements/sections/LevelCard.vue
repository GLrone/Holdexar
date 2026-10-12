<script setup lang="ts">
/* 等级身份卡：等级环 + 段位陈列 + 本级进度 + 成本累增对比 + 境界节点 + 近期经验。
   曲线、段位、加成全部消费后端派生字段，本地只做展示换算（不推算业务口径）。 */
import { computed, ref, watch } from 'vue'

import type { LevelTier, XpEventSource } from '@/api/client'
import { HlSkeleton } from '@/components/ui'
import type { MessageKey } from '@/locales'
import { useI18n } from '@/locales'
import { useTrophyHallStore } from '@/stores/trophyHall'
import TrophyMedal from '@/components/business/TrophyMedal.vue'

const { t } = useI18n()
const store = useTrophyHallStore()

const TIER_KEYS: Record<LevelTier, MessageKey> = {
  bronze: 'achievements.level.tier.bronze',
  silver: 'achievements.level.tier.silver',
  gold: 'achievements.level.tier.gold',
  platinum: 'achievements.level.tier.platinum',
  legend: 'achievements.level.tier.legend',
}

const EVENT_KEYS: Record<XpEventSource, MessageKey> = {
  achievement_unlock: 'achievements.level.event.achievementUnlock',
  platinum: 'achievements.level.event.platinum',
  playtime_bucket: 'achievements.level.event.playtimeBucket',
  streak_day: 'achievements.level.event.streakDay',
  challenge_claim: 'achievements.level.event.challengeClaim',
  comment: 'achievements.level.event.comment',
  baseline: 'achievements.level.event.baseline',
}

/* 段位五档 → 奖杯陈列件四档（legend 挂白金件） */
const MEDAL_TIER: Record<LevelTier, 'platinum' | 'gold' | 'silver' | 'bronze'> = {
  bronze: 'bronze',
  silver: 'silver',
  gold: 'gold',
  platinum: 'platinum',
  legend: 'platinum',
}

const level = computed(() => store.level)

const levelPct = computed(() => Math.round((level.value?.levelProgress ?? 0) * 1000) / 10)
const growthPct = computed(() => Math.round((level.value?.nextCostGrowth ?? 0) * 1000) / 10)

/* 成本对比两根条共用同一标尺（大者满宽），「越升越贵」直读 */
const costPair = computed(() => {
  const last = level.value?.lastLevelCost ?? 0
  const next = level.value?.xpForNextLevel ?? 0
  const max = Math.max(last, next, 1)
  return {
    last: Math.round((last / max) * 100),
    next: Math.round((next / max) * 100),
  }
})

const curveMax = computed(() => Math.max(1, ...(level.value?.curvePreview ?? []).map((p) => p.costXp)))
function curveHeight(costXp: number): number {
  return Math.max(6, Math.round((costXp / curveMax.value) * 100))
}

/** 第一个未达成的境界节点下标（-1 = 全部达成） */
const nextMilestone = computed(() => (level.value?.milestones ?? []).findIndex((m) => !m.reached))

/* 涨级反馈：卡内等级数字跳一次 pulse（岛消息走事实通道，不在这里重复弹） */
const pulse = ref(false)
let pulseTimer = 0
watch(
  () => level.value?.level,
  (v, old) => {
    if (v == null || !old || v <= old) return
    pulse.value = true
    window.clearTimeout(pulseTimer)
    pulseTimer = window.setTimeout(() => {
      pulse.value = false
    }, 700)
  },
)

function fmt(n: number): string {
  return n.toLocaleString()
}

function fmtMult(m: number): string {
  return String(Math.round(m * 100) / 100)
}

function bonusPct(multiplier: number): number {
  return Math.round((multiplier - 1) * 100)
}

function fmtWhen(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' })
}
</script>

<template>
  <div>
    <HlSkeleton v-if="!level && store.loading" variant="card" :count="1" />
    <p v-else-if="!level && store.error" class="cr-note lvl-error">{{ store.error }}</p>
    <div v-else-if="level" class="cr-card lvl-card">
      <!-- 身份行：等级环 + 段位陈列 + 活跃加成 -->
      <div class="lvl-hero">
        <div class="lvl-ring" :style="{ '--p': String(levelPct) }">
          <span class="lvl-num hl-num" :class="{ 'is-pulse': pulse }">{{ level.level }}</span>
        </div>
        <div class="lvl-id">
          <span class="lvl-title">{{ t('achievements.level.card.title') }}</span>
          <span class="lvl-tier">
            <TrophyMedal :tier="MEDAL_TIER[level.tier]" :size="26" glow />
            {{ t(TIER_KEYS[level.tier]) }}
            <span v-if="level.nextTier" class="lvl-tier-next">→ {{ t(TIER_KEYS[level.nextTier]) }}</span>
          </span>
          <span class="lvl-total">
            {{ t('achievements.level.card.totalXp') }}
            <b class="hl-num">{{ fmt(level.totalXp) }}</b>
          </span>
        </div>
        <div v-if="level.activeBonuses.length" class="lvl-bonuses">
          <span
            v-for="b in level.activeBonuses"
            :key="b.kind"
            class="lvl-bonus"
          >
            {{ t('achievements.level.bonus.streak', { days: b.streakDays, percent: bonusPct(b.multiplier) }) }}
          </span>
        </div>
      </div>

      <!-- 本级进度 -->
      <div class="lvl-progress">
        <div class="lvl-progress-head">
          <span>{{ t('achievements.level.card.progressLabel') }}</span>
          <span class="hl-num">{{ fmt(level.xpIntoLevel) }} / {{ fmt(level.xpForNextLevel) }}</span>
        </div>
        <div class="lvl-progress-bar"><i :style="{ width: levelPct + '%' }" /></div>
        <span class="lvl-remaining">
          {{ t('achievements.level.card.nextLevel', { n: level.level + 1, xp: fmt(level.xpRemaining) }) }}
        </span>
      </div>

      <!-- 成本累增对比 + 未来成本曲线 -->
      <div class="lvl-cols">
        <div class="lvl-cost">
          <p class="lvl-block-title">{{ t('achievements.level.card.costTitle') }}</p>
          <div class="lvl-cost-row">
            <span>{{ t('achievements.level.card.lastCost') }}</span>
            <div class="lvl-cost-bar"><i :style="{ width: costPair.last + '%' }" /></div>
            <b class="hl-num">{{ fmt(level.lastLevelCost) }}</b>
          </div>
          <div class="lvl-cost-row">
            <span>{{ t('achievements.level.card.nextCost') }}</span>
            <div class="lvl-cost-bar"><i class="is-next" :style="{ width: costPair.next + '%' }" /></div>
            <b class="hl-num">{{ fmt(level.xpForNextLevel) }}</b>
            <span class="lvl-growth hl-num">{{ t('achievements.level.card.costGrowth', { pct: growthPct }) }}</span>
          </div>
        </div>
        <div class="lvl-curve">
          <p class="lvl-block-title">{{ t('achievements.level.card.curveTitle') }}</p>
          <div class="lvl-curve-bars">
            <div
              v-for="p in level.curvePreview"
              :key="p.level"
              class="lvl-curve-col"
              :title="t('achievements.level.card.curveBar', { level: p.level, xp: fmt(p.costXp) })"
            >
              <i :style="{ height: curveHeight(p.costXp) + '%' }" />
              <b class="hl-num">{{ p.level }}</b>
            </div>
          </div>
        </div>
      </div>

      <!-- 境界节点：已过 / 下一境界 / 未来 -->
      <div class="lvl-ms">
        <p class="lvl-block-title">{{ t('achievements.level.card.milestonesTitle') }}</p>
        <div class="lvl-ms-track">
          <template v-for="(m, i) in level.milestones" :key="m.level">
            <span
              v-if="i > 0"
              class="lvl-ms-line"
              :class="{ 'is-done': level.milestones[i - 1].reached }"
            />
            <div class="lvl-ms-node" :class="{ 'is-done': m.reached, 'is-next': i === nextMilestone }">
              <i class="lvl-ms-dot" />
              <b class="hl-num">{{ m.level }}</b>
              <span v-if="i === nextMilestone" class="lvl-ms-tag">
                {{ t('achievements.level.card.milestoneNext') }}
              </span>
            </div>
          </template>
        </div>
      </div>

      <!-- 近期经验摘要 -->
      <div class="lvl-recent">
        <p class="lvl-block-title">{{ t('achievements.level.card.recentTitle') }}</p>
        <div v-if="level.recentEvents.length" class="lvl-recent-list">
          <div v-for="e in level.recentEvents" :key="e.id" class="lvl-event">
            <span class="lvl-event-src">{{ t(EVENT_KEYS[e.source]) }}</span>
            <span v-if="e.multiplier > 1" class="lvl-event-mult hl-num">×{{ fmtMult(e.multiplier) }}</span>
            <span class="lvl-event-when hl-num">{{ fmtWhen(e.createdAt) }}</span>
            <span class="lvl-event-xp hl-num">{{ t('achievements.level.card.xpGain', { xp: fmt(e.xp) }) }}</span>
          </div>
        </div>
        <p v-else class="cr-note">{{ t('achievements.level.card.recentEmpty') }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.lvl-error { color: var(--danger); }

/* 等级环：conic 令牌色进度 + 内圆挖空 */
.lvl-ring {
  position: relative;
  width: 84px;
  height: 84px;
  border-radius: 50%;
  background: conic-gradient(var(--accent-fill) calc(var(--p, 0) * 1%), var(--surface-track) 0);
  display: grid;
  place-items: center;
  flex-shrink: 0;
}

.lvl-ring::before {
  content: '';
  position: absolute;
  inset: 7px;
  border-radius: 50%;
  background: var(--bg-card);
}

.lvl-num {
  position: relative;
  z-index: 1;
  font-size: 27px;
  font-weight: 800;
  color: var(--text-primary);
}

@keyframes lvlPulse {
  0% { transform: scale(1); }
  40% { transform: scale(1.22); text-shadow: 0 0 16px var(--accent-a45); }
  100% { transform: scale(1); }
}

.lvl-num.is-pulse {
  animation: lvlPulse calc(var(--duration-5) * var(--motion-scale) * 1.5) var(--ease-out);
}

.lvl-hero { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.lvl-id { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.lvl-title { font-size: 13px; font-weight: 700; color: var(--text-primary); }
.lvl-tier { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px; font-weight: 600; color: var(--text-secondary); }
.lvl-tier-next { font-weight: 400; color: var(--text-dim); }
.lvl-total { font-size: 11px; color: var(--text-dim); }
.lvl-total b { margin-left: 4px; color: var(--text-primary); }

.lvl-bonuses { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; margin-left: auto; }
.lvl-bonus {
  font-size: 10.5px;
  font-weight: 700;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-radius: 999px;
  padding: 2px 9px;
  white-space: nowrap;
}

.lvl-progress { margin-top: 10px; }
.lvl-progress-head { display: flex; justify-content: space-between; gap: 8px; font-size: 10.5px; color: var(--text-dim); }
.lvl-progress-bar {
  height: 7px;
  border-radius: 4px;
  background: var(--surface-track);
  overflow: hidden;
  margin: 5px 0 3px;
}
.lvl-progress-bar i {
  display: block;
  height: 100%;
  border-radius: 4px;
  background: var(--accent-fill);
  transition: width calc(var(--duration-4) * var(--motion-scale)) var(--ease-out);
}
.lvl-remaining { font-size: 10px; color: var(--text-dim); }

.lvl-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 24px; margin-top: 12px; }
.lvl-block-title {
  margin: 0;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--text-dim);
}

.lvl-cost-row {
  display: grid;
  grid-template-columns: 74px 1fr auto;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  font-size: 10.5px;
  color: var(--text-secondary);
}
.lvl-cost-bar { height: 6px; border-radius: 3px; background: var(--surface-track); overflow: hidden; }
.lvl-cost-bar i { display: block; height: 100%; border-radius: 3px; background: var(--text-dim); }
.lvl-cost-bar i.is-next { background: var(--accent-fill); }
.lvl-cost-row b { min-width: 46px; text-align: right; font-size: 11px; font-weight: 700; color: var(--text-primary); }
.lvl-growth { font-size: 9.5px; font-weight: 700; color: var(--warning); }

.lvl-curve-bars { display: flex; align-items: stretch; gap: 10px; height: 66px; margin-top: 8px; }
.lvl-curve-col {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: center;
  gap: 3px;
}
.lvl-curve-col i {
  width: 100%;
  max-width: 26px;
  border-radius: 3px 3px 0 0;
  background: var(--accent-fill);
  opacity: 0.85;
}
.lvl-curve-col b { font-size: 9px; font-weight: 600; color: var(--text-dim); }

.lvl-ms { margin-top: 12px; }
.lvl-ms-track { display: flex; align-items: flex-start; margin-top: 8px; }
.lvl-ms-node {
  position: relative;
  width: 36px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
}
.lvl-ms-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid var(--surface-track);
  background: var(--bg-card);
}
.lvl-ms-node.is-done .lvl-ms-dot {
  background: var(--accent-fill);
  border-color: var(--accent-fill);
}
.lvl-ms-node.is-next .lvl-ms-dot {
  border-color: var(--accent-fill);
  box-shadow: 0 0 0 3px var(--accent-a30);
}
.lvl-ms-node b { font-size: 10px; font-weight: 600; color: var(--text-dim); }
.lvl-ms-node.is-done b, .lvl-ms-node.is-next b { color: var(--text-primary); }
.lvl-ms-tag { font-size: 9px; font-weight: 700; color: var(--accent); white-space: nowrap; }
.lvl-ms-line { flex: 1; height: 2px; margin-top: 4px; background: var(--surface-track); }
.lvl-ms-line.is-done { background: var(--accent-fill); }

.lvl-recent { margin-top: 12px; }
.lvl-recent-list {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 18px;
  margin-top: 8px;
  max-height: 118px;
  overflow-y: auto;
}
.lvl-event { display: flex; align-items: baseline; gap: 6px; min-width: 0; font-size: 10.5px; }
.lvl-event-src {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}
.lvl-event-mult { flex-shrink: 0; font-size: 9.5px; font-weight: 700; color: var(--accent); }
.lvl-event-when { flex-shrink: 0; margin-left: auto; font-size: 9.5px; color: var(--text-dim); }
.lvl-event-xp { flex-shrink: 0; font-weight: 700; color: var(--success); }

@media (max-width: 820px) {
  .lvl-cols, .lvl-recent-list { grid-template-columns: 1fr; }
  .lvl-bonuses { margin-left: 0; align-items: flex-start; }
}
</style>
