<script setup lang="ts">
/* 玩家名片：头像 + 用户名 + 一枚**彩虹渐变发光艺术字称号**，两侧配麦穗桂冠。
 *
 *   · 深蓝底 + 右侧隐约透出的暗化底图——用「时长最高的一款游戏封面」当底图，
 *     压暗后从右向左渐隐，这张名片讲的就是玩家自己的生涯；
 *   · 右上角绿色圆角徽章 → 生涯区间（首枚成就 → 最近一次解锁）；
 *   · 中央圆形头像 + 用户名；
 *   · 最下方彩虹渐变发光称号 = 已获得称号里档位最高的那一枚。
 *
 * 称号取值直接复用称号墙的评测结果（evaluateTitles 已按「已达成 → 档位 → 进度」
 * 排序），所以名片上的称号与称号墙第一张卡永远是同一枚，不会两处各说各话。
 */
import { computed } from 'vue'

import type { CareerPayload } from '@/api/client'
import { HlIcon, HlImg } from '@/components/ui'
import { useI18n } from '@/locales'
import { careerScore, evaluateTitleCards } from '@/lib/careerTitles'
import { useAccountStore } from '@/stores/account'

const props = defineProps<{ career: CareerPayload }>()

const { t } = useI18n()
const account = useAccountStore()

/** 名片评测只跑一次：名片上的称号与称号墙首卡必须是同一张（已按阶位排序），
 *  彩虹艺术字取**当前阶位最高**的那张称号名片 */
const cards = computed(() => evaluateTitleCards(props.career))
const featured = computed(() => cards.value.find((x) => x.level >= 1) ?? null)
const earnedCount = computed(() => cards.value.filter((x) => x.level >= 1).length)

/** 底图：时长最高的一款（名片右侧隐约透出的那张） */
const backdrop = computed(() => props.career.spotlight[0]?.headerImage ?? '')

const playerName = computed(
  () => account.status?.profile?.persona_name || t('achievements.career.card.player'),
)
const avatarUrl = computed(() => account.status?.profile?.avatar_url || '')

const span = computed(() => {
  const from = props.career.activity.firstDate
  const to = props.career.activity.lastDate
  if (!from || !to) return ''
  const y1 = from.slice(0, 4)
  const y2 = to.slice(0, 4)
  return y1 === y2 ? y1 : t('achievements.career.card.span', { from: y1, to: y2 })
})

/** 名片徽章跟随五级阶梯制：显示总阶 */
const score = computed(() => careerScore(props.career).level)

/** 麦穗叶片：沿一条弧线排布的 6 片（左右镜像各一份，纯 SVG 手绘） */
const LAUREL_LEAVES = [
  { x: 4.6, y: 30.5, r: -32 },
  { x: 6.2, y: 25.0, r: -22 },
  { x: 8.4, y: 19.6, r: -12 },
  { x: 11.4, y: 14.6, r: -2 },
  { x: 15.2, y: 10.4, r: 8 },
  { x: 19.6, y: 7.2, r: 20 },
]
</script>

<template>
  <div class="cr-player">
    <!-- 右侧暗化底图：mask 从右向左渐隐，只"隐约透出" -->
    <HlImg class="cr-player-art" :src="backdrop" alt="" loading="lazy">
      <template #fallback><span class="cr-player-art-fb" /></template>
    </HlImg>

    <span v-if="span" class="cr-player-badge">
      <HlIcon name="calendar" />
      {{ t('achievements.career.card.badge') }} · {{ span }}
    </span>

    <div class="cr-player-body">
      <div class="cr-player-avatar">
        <HlImg class="cr-player-avatar-img" :src="avatarUrl" alt="" loading="lazy">
          <template #fallback>
            <span class="cr-player-avatar-fb">{{ playerName.slice(0, 1) }}</span>
          </template>
        </HlImg>
      </div>

      <div class="cr-player-name">{{ playerName }}</div>

      <div class="cr-player-medals">
        <span class="cr-chip">{{ t('achievements.career.card.medals', { n: earnedCount }) }}</span>
        <span class="cr-chip">{{ t('achievements.career.card.level', { n: score }) }}</span>
      </div>

      <!-- 彩虹渐变发光称号 + 两侧麦穗桂冠 -->
      <div class="cr-player-title">
        <svg class="cr-laurel" viewBox="0 0 26 36" aria-hidden="true">
          <path class="cr-laurel-stem" d="M21 34C11.5 30 7 21.5 8.6 8.4" />
          <ellipse
            v-for="(leaf, i) in LAUREL_LEAVES"
            :key="i"
            class="cr-laurel-leaf"
            :cx="leaf.x"
            :cy="leaf.y"
            rx="3.4"
            ry="1.7"
            :transform="`rotate(${leaf.r} ${leaf.x} ${leaf.y})`"
          />
        </svg>

        <span v-if="featured" class="cr-player-titletext">{{ t(featured.nameKey) }}</span>
        <span v-else class="cr-player-notitle">{{ t('achievements.career.card.noTitle') }}</span>

        <svg class="cr-laurel cr-laurel--flip" viewBox="0 0 26 36" aria-hidden="true">
          <path class="cr-laurel-stem" d="M21 34C11.5 30 7 21.5 8.6 8.4" />
          <ellipse
            v-for="(leaf, i) in LAUREL_LEAVES"
            :key="i"
            class="cr-laurel-leaf"
            :cx="leaf.x"
            :cy="leaf.y"
            rx="3.4"
            ry="1.7"
            :transform="`rotate(${leaf.r} ${leaf.x} ${leaf.y})`"
          />
        </svg>
      </div>

      <p class="cr-player-legend">{{ t('achievements.career.card.titleLabel') }}</p>
    </div>
  </div>
</template>

<style scoped>
.cr-player {
  position: relative;
  padding: 14px 16px 16px;
  border-radius: var(--radius-lg);
  background: linear-gradient(135deg, var(--report-bg-1) 0%, var(--report-bg-2) 62%, var(--report-bg-3) 100%);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

/* 右侧原画：压暗 + 右侧对齐 + 自右向左渐隐 */
.cr-player-art {
  position: absolute;
  right: 0;
  top: 0;
  height: 100%;
  width: 58%;
  object-fit: cover;
  object-position: center;
  opacity: 0.4;
  filter: saturate(0.55) brightness(0.62);
  -webkit-mask-image: linear-gradient(90deg, transparent 0%, var(--report-fg) 78%);
  mask-image: linear-gradient(90deg, transparent 0%, var(--report-fg) 78%);
  pointer-events: none;
}

.cr-player-art-fb {
  display: block;
  width: 100%;
  height: 100%;
}

.cr-player-badge {
  position: absolute;
  right: 14px;
  top: 12px;
  z-index: 2;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 9px;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 700;
  color: var(--report-bg-1);
  background: var(--report-neon-5);
}

.cr-player-body {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 7px;
  padding-top: 4px;
}

.cr-player-avatar {
  width: 84px;
  height: 84px;
  border-radius: 50%;
  overflow: hidden;
  border: 2px solid var(--gild-2);
  box-shadow: 0 0 18px -2px var(--gild-2);
  background: var(--report-glass);
}

.cr-player-avatar-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.cr-player-avatar-fb {
  display: grid;
  place-items: center;
  width: 100%;
  height: 100%;
  font-size: 30px;
  font-weight: 800;
  color: var(--report-fg);
}

.cr-player-name {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.02em;
  color: var(--report-fg);
  text-shadow: 0 2px 10px var(--report-glass);
}

.cr-player-medals {
  display: flex;
  gap: 6px;
}

.cr-chip {
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--report-line);
  background: var(--report-glass);
  font-size: 10px;
  color: var(--report-fg-dim);
}

/* 称号行：桂冠 + 彩虹艺术字 */
.cr-player-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.cr-laurel {
  width: 22px;
  height: 30px;
  color: var(--report-fg);
  opacity: 0.9;
  flex: 0 0 auto;
}

.cr-laurel--flip {
  transform: scaleX(-1);
}

.cr-laurel-stem {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
}

.cr-laurel-leaf {
  fill: currentColor;
}

.cr-player-titletext {
  font-size: 26px;
  font-weight: 800;
  letter-spacing: 0.04em;
  /* 彩虹渐变艺术字：底色渐变 + 裁进文字 + 透明字色，再叠一层外发光 */
  background-image: linear-gradient(
    92deg,
    var(--report-neon-6) 0%,
    var(--report-neon-1) 22%,
    var(--report-neon-5) 44%,
    var(--report-neon-2) 66%,
    var(--report-neon-3) 84%,
    var(--report-neon-4) 100%
  );
  background-size: 220% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  filter: drop-shadow(0 0 10px var(--report-glass));
  animation: crTitleFlow calc(var(--duration-5) * var(--motion-scale) * 16) linear infinite;
}

@keyframes crTitleFlow {
  0% {
    background-position: 0% 50%;
  }

  100% {
    background-position: 220% 50%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .cr-player-titletext {
    animation: none;
  }
}

.cr-player-notitle {
  font-size: 16px;
  font-weight: 700;
  color: var(--report-fg-faint);
}

.cr-player-legend {
  margin: 0;
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--report-fg-faint);
}
</style>