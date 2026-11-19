<script setup lang="ts">
/**
 * HB 当月包卡片 —— Humble Choice 本月内容一览，仪表盘分节卡。
 *
 * 数据链：GET /metadata/hb/offers（纯本地库读：记账游标 → 当月标签 →
 * games.is_hb 标记行 + 国区价）→ 挂载即拉 + 每小时轻刷（抓取链每日跑，
 * 库内标记到位后卡片自动浮出）。「立即刷新」走 POST /metadata/hb/refresh
 * 强制重打标（绕过月包判重）后重读清单，空态/异常时的手动恢复入口。
 * 游戏卡点击进站内详情页；头部「跳过本月 / 前往月包」是官方页直达外链。
 * 归属徽章与卡片归属边框（已拥有/家庭共享/愿望单）走 ownership 批量接口，
 * 与游戏库同源配色与词条。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { metadataApi, type HbChoiceGame } from '@/api/client'
import { formatCnyFen } from '@/api/regions'
import { useI18n, type MessageKey } from '@/locales'
import { useOwnershipStore, type OwnershipInfo, type OwnershipType } from '@/stores/ownership'
import { HlAvatar, HlButton, HlEmpty, HlImg, HlSkeleton } from '@/components/ui'

const router = useRouter()
const { t } = useI18n()
const ownershipStore = useOwnershipStore()

const games = ref<HbChoiceGame[]>([])
const label = ref('')
const monthUrl = ref('')
const skipUrl = ref('')
/** loading=首次拉取中 / ok=有数据 / failed=无数据可显（尚未入库或拉取失败） */
const state = ref<'loading' | 'ok' | 'failed'>('loading')
/** 手动强刷进行中（按钮转圈防重入） */
const refreshing = ref(false)
/** 最近一次手动刷新的失败原因（成功即清空；空态卡片与头部下沿各可呈现） */
const refreshNote = ref('')

/** 非 Steam 平台标识 → 本地化名；未知平台回传原值（新平台无需改码即显示） */
const PLATFORM_LABEL_KEYS: Record<string, MessageKey> = {
  steam: 'hbChoice.platform.steam',
  blizzard: 'hbChoice.platform.blizzard',
  epic: 'hbChoice.platform.epic',
  ubisoft: 'hbChoice.platform.ubisoft',
  gog: 'hbChoice.platform.gog',
  origin: 'hbChoice.platform.origin',
  ea: 'hbChoice.platform.ea',
  itch: 'hbChoice.platform.itch',
  'drm-free': 'hbChoice.platform.drm-free',
}

function platformLabel(platform: string): string {
  const key = PLATFORM_LABEL_KEYS[platform]
  return key ? t(key) : platform
}

/** 非 Steam 平台游戏：appid=0，无站内详情页，不参与归属/价格体系 */
function isPlatformGame(g: HbChoiceGame): boolean {
  return Boolean(g.platform) && g.appid === 0
}

async function load() {
  try {
    const res = await metadataApi.hbChoiceOffers()
    if (res.ok && res.games.length > 0) {
      games.value = res.games
      label.value = res.label
      monthUrl.value = res.monthUrl
      skipUrl.value = res.skipUrl
      state.value = 'ok'
      // 归属徽章按需取数：同帧合并为一次批量请求（与游戏库同 store 缓存）；
      // 非 Steam 平台游戏（appid=0）无归属体系，跳过
      games.value.forEach((g) => {
        if (g.appid > 0) ownershipStore.ensure(g.appid)
      })
    } else if (games.value.length === 0) {
      state.value = 'failed'
    }
  } catch {
    if (games.value.length === 0) state.value = 'failed'
  }
}

/** 手动强刷：强制重打标后重读清单（空态卡片的恢复入口）。失败必须可见，
 *  静默吞掉会让「点了没反应」被当成假刷新。 */
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  refreshNote.value = ''
  try {
    const res = await metadataApi.hbRefresh()
    if (!res.ok) refreshNote.value = res.error || t('hbChoice.refreshFailed')
  } catch {
    refreshNote.value = t('hbChoice.refreshFailed')
  }
  await load()
  refreshing.value = false
}

// ─── 归属徽章（与游戏库 status-badge 同源词条/配色；存 key 渲染期 t()）───

const STATUS_BADGE_KEYS: Record<OwnershipType, MessageKey> = {
  owned: 'gameCard.status.owned',
  family: 'gameCard.status.family',
  wishlist: 'gameCard.status.wishlist',
}

function ownershipOf(g: HbChoiceGame): OwnershipInfo | null {
  return ownershipStore.map[g.appid] ?? null
}

/** 多账号共同愿望单 → 愿望单 +N（口径同游戏库） */
function badgeText(o: OwnershipInfo): string {
  if (o.type === 'wishlist' && o.owners.length > 1) {
    return t('gameCard.status.wishlistMore', { n: o.owners.length - 1 })
  }
  return t(STATUS_BADGE_KEYS[o.type])
}

/** 悬停提示：归属维度 + 账号名（沿用游戏商店归属弹窗视觉，Teleport 落 body） */
const STATUS_META: Record<OwnershipType, { titleKey: MessageKey; color: string; border: string }> = {
  owned: { titleKey: 'gameCard.status.ownedTitle', color: 'var(--success)', border: 'var(--success-a40)' },
  family: { titleKey: 'gameCard.status.familyTitle', color: 'var(--purple)', border: 'var(--purple-a30)' },
  wishlist: { titleKey: 'gameCard.status.wishlistTitle', color: 'var(--accent)', border: 'var(--accent-a40)' },
}

/** 归属弹窗状态（悬停归属徽章弹出，账号行 + 头像；离开 120ms 延迟收场） */
const statusTip = ref<{
  type: OwnershipType
  owners: string[]
  avatars: string[]
  left: number
  top: number
} | null>(null)
let statusTipTimer: number | null = null

function statusTipEnter(e: MouseEvent, g: HbChoiceGame) {
  const ownership = ownershipOf(g)
  if (!ownership) return
  if (statusTipTimer !== null) {
    window.clearTimeout(statusTipTimer)
    statusTipTimer = null
  }
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  let left = rect.left
  if (left + 160 > window.innerWidth) left = window.innerWidth - 170
  const owners = ownership.owners
  statusTip.value = {
    type: ownership.type,
    owners: owners.length > 0 ? owners : [t('gameCard.owner.unknown')],
    avatars:
      owners.length > 0
        ? owners.map((_, i) => ownership.ownerAvatars?.[i] || '')
        : [''],
    left: Math.max(8, left),
    top: rect.bottom + 6,
  }
}

function statusTipLeave() {
  if (statusTipTimer !== null) window.clearTimeout(statusTipTimer)
  statusTipTimer = window.setTimeout(() => {
    statusTip.value = null
    statusTipTimer = null
  }, 120)
}

const HOUR_MS = 3_600_000
let timer: number | undefined

onMounted(() => {
  load()
  timer = window.setInterval(load, HOUR_MS)
})

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
})
</script>

<template>
  <div class="card hb-choice" data-section="dashboard.section.hbChoice">
    <div class="hb-choice__header">
      <div class="hb-choice__title">
        <img src="/assets/logo_hb.ico" alt="" class="hb-choice__logo" />
        {{ t('hbChoice.title') }}
        <span v-if="label" class="hb-choice__label">{{ label }}</span>
      </div>
      <div class="hb-choice__meta">
        <HlButton
          variant="text"
          size="sm"
          :loading="refreshing"
          class="hb-choice__refresh"
          @click="refresh"
        >{{ refreshing ? t('hbChoice.refreshing') : t('hbChoice.refresh') }}</HlButton>
        <a
          v-if="skipUrl"
          class="hb-choice__link"
          :href="skipUrl"
          target="_blank"
          rel="noopener noreferrer"
        >{{ t('hbChoice.skip') }} ↗</a>
        <a
          v-if="monthUrl"
          class="hb-choice__link"
          :href="monthUrl"
          target="_blank"
          rel="noopener noreferrer"
        >{{ t('hbChoice.goMonth') }} ↗</a>
      </div>
    </div>

    <HlSkeleton
      v-if="state === 'loading'"
      variant="card"
      :count="4"
      class="hb-choice__skeleton"
    />
    <HlEmpty
      v-else-if="state === 'failed'"
      size="sm"
      icon=""
      :text="t('hbChoice.empty')"
    />
    <div v-else class="hb-choice__grid">
      <div
        v-for="g in games"
        :key="g.appid || `p-${g.name}`"
        class="hb-card"
        :class="[ownershipOf(g)?.type, { 'hb-card--external': isPlatformGame(g) }]"
        @click="isPlatformGame(g) || router.push(`/game/${g.appid}`)"
      >
        <div class="hb-card__media">
          <HlImg class="hb-card__img" :src="g.headerImage" :alt="g.name" loading="lazy" />
          <span v-if="g.discount > 0" class="hb-card__off">-{{ g.discount }}%</span>
          <!-- 非 Steam 平台徽章：Battle.net/Epic/育碧…（后端下发原值，本地化渲染） -->
          <span v-if="g.platform" class="hb-card__platform">{{ platformLabel(g.platform) }}</span>
          <!-- 归属徽章（已拥有/家庭共享/愿望单）：沿用游戏商店归属弹窗
               （Teleport 账号行 + 头像），不再走 native title 气泡 -->
          <span
            v-if="ownershipOf(g)"
            class="hb-card__status"
            :class="ownershipOf(g)!.type"
            @mouseenter="statusTipEnter($event, g)"
            @mouseleave="statusTipLeave()"
          >{{ badgeText(ownershipOf(g)!) }}</span>
        </div>
        <div class="hb-card__body">
          <span class="hb-card__name">{{ g.name }}</span>
          <span class="hb-card__price">
            <template v-if="g.priceFen != null">
              <span class="hb-card__now">{{ formatCnyFen(g.priceFen) }}</span>
              <span
                v-if="g.originalPriceFen != null && g.discount > 0"
                class="hb-card__orig"
              >{{ formatCnyFen(g.originalPriceFen) }}</span>
            </template>
            <template v-else-if="g.originalPriceFen != null">
              <span class="hb-card__now">{{ formatCnyFen(g.originalPriceFen) }}</span>
            </template>
            <span v-else class="hb-card__now hb-card__now--none">—</span>
          </span>
        </div>
      </div>
    </div>
    <p v-if="refreshNote" class="hb-choice__note">{{ refreshNote }}</p>

    <!-- 归属弹窗（沿用游戏商店 cf-status-tip 同款视觉与全局样式） -->
    <Teleport to="body">
      <div
        v-if="statusTip"
        class="cf-status-tip"
        :style="{ left: statusTip.left + 'px', top: statusTip.top + 'px', borderColor: STATUS_META[statusTip.type].border }"
      >
        <div class="cf-status-tip__title" :style="{ color: STATUS_META[statusTip.type].color }">
          {{ t(STATUS_META[statusTip.type].titleKey) }}
        </div>
        <div class="cf-status-tip__list">
          <div
            v-for="(name, i) in statusTip.owners"
            :key="`${name}-${i}`"
            class="cf-status-tip__row"
          >
            <HlAvatar :src="statusTip.avatars[i]" :name="name" size="sm" square />
            <span class="cf-status-tip__name">{{ name }}</span>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.hb-choice {
  padding: 18px 20px;
}

.hb-choice__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.hb-choice__title {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.hb-choice__logo {
  display: block;
  width: 18px;
  height: 18px;
}

.hb-choice__note {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

/* 当月标签（后端下发的事实数据，非界面文案） */
.hb-choice__label {
  flex-shrink: 1;
  min-width: 0;
  padding: 1px 8px;
  border-radius: var(--radius-sm);
  background: var(--warning-a15);
  color: var(--warning);
  font-size: 11px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hb-choice__meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

/* 手动强刷按键：与外链同排同号字，accent 弱化避免抢过外链 */
.hb-choice__refresh {
  font-size: 12px;
}

/* 官方页直达外链（真实链接语义，非动作按键） */
.hb-choice__link {
  font-size: 12px;
  color: var(--accent);
  text-decoration: none;
}

.hb-choice__link:hover {
  text-decoration: underline;
}

.hb-choice__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 12px;
}

.hb-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--bg-soft);
  cursor: pointer;
  transition:
    transform var(--transition),
    border-color var(--transition),
    box-shadow var(--transition);
}

.hb-card:hover {
  transform: translateY(-2px);
  border-color: var(--accent);
  box-shadow: var(--shadow-sm);
}

/* 归属边框（与游戏库 game-card 同一套口径：2px 同色描边 + 同色系渐变底，
   令牌跟随双主题）。置于 :hover 之后：同特异性下后声明胜出，悬停抬升
   不改变归属描边色。 */
.hb-card.owned {
  border: 2px solid var(--success-a60);
  background: linear-gradient(135deg, var(--surface-overlay-panel), var(--success-a08));
}

.hb-card.family {
  border: 2px solid var(--purple-a60);
  background: linear-gradient(135deg, var(--surface-overlay-panel), var(--purple-a08));
}

.hb-card.wishlist {
  border: 2px solid var(--accent-a50);
  background: linear-gradient(135deg, var(--surface-overlay-panel), var(--accent-a08));
}

.hb-card__media {
  position: relative;
  aspect-ratio: 16 / 9;
  background: var(--bg-soft);
}

.hb-card__img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* 三段平滑沉底：与仪表盘轮播同一手法 */
.hb-card__media::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    rgba(0, 0, 0, 0) 55%,
    rgba(0, 0, 0, 0.45) 100%
  );
  pointer-events: none;
}

.hb-card__off {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 1;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--danger-a15);
  color: var(--danger-on-dark);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
}

/* 非 Steam 平台徽章（右上角；归属徽章存在时顺移避让）：实底深墨 + 白字，
   封面上任何主题都保持可读 */
.hb-card__platform {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 1;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--ink-on-fill);
  color: var(--text-on-fill);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.hb-card__status ~ .hb-card__platform,
.hb-card__platform ~ .hb-card__status {
  top: 34px;
}

/* 非 Steam 平台游戏：无站内详情页，卡片不可点击进入 */
.hb-card--external {
  cursor: default;
}

/* 归属徽章（右上角，与左上折扣徽章对称）：实底语义色 + on-accent 对比字
   （亮色主题白字、暗色主题深墨），封面上不再用透底彩字 */
.hb-card__status {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 1;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: 11px;
  font-weight: 700;
  cursor: help;
  color: var(--on-accent);
}

.hb-card__status.owned {
  background: var(--success);
}

.hb-card__status.family {
  background: var(--purple);
}

.hb-card__status.wishlist {
  background: var(--accent);
}

.hb-card__body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px 10px;
}

.hb-card__name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hb-card__price {
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
  flex-shrink: 0;
}

.hb-card__now {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

/* 无价格行（占位行待回补）：占位符不强调 */
.hb-card__now--none {
  font-weight: 400;
  color: var(--text-muted);
}

.hb-card__orig {
  font-size: 11px;
  color: var(--text-muted);
  text-decoration: line-through;
}
</style>
