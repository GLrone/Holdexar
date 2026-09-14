<script setup lang="ts">
/**
 * Epic 喜加一卡片组 —— 当期在送 + 下周预告 + 移动端白送，仪表盘分节卡。
 *
 * 数据链：GET /metadata/epic/offers（服务端快照缓存 30 分钟 + 过期后台刷新，
 * 只读展示链，不做 appid 匹配不落游戏行）→ 前端挂载即拉 + 每小时轮询；
 * 冷启动先显落库快照（stale + 「刷新中」），10s 短轮询盯到后台刷新落点
 * 自动覆盖，无需等一轮完整抓取。
 * 三种卡刻意不同形（防误读）：
 * - 当期：绿「免费」章 + 「前往领取 ↗」——真实可领，整卡外链商店页；
 * - 预告：去领取 CTA、封面去饱和、虚线框——只传达「N 天后开始」；
 * - 移动端：无名称/日期（官方无结构化出口，立绘即信息），链指官方移动页，
 *   明示「App 内领取」——PC 商店页出不来免费按钮，不与 PC 领取混同。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { metadataApi, type EpicMobileOffer, type EpicOffer } from '@/api/client'
import { useI18n } from '@/locales'
import { HlEmpty, HlImg, HlSkeleton } from '@/components/ui'

const { t } = useI18n()

const offers = ref<EpicOffer[]>([])
const mobile = ref<EpicMobileOffer | null>(null)
const fetchedAt = ref('')
/** loading=首次拉取中 / ok=有数据（含失败时保留的旧数据）/ failed=无数据可显 */
const state = ref<'loading' | 'ok' | 'failed'>('loading')
/** true = 当前显示的是过期快照，服务端后台正在刷新（短轮询覆盖） */
const stale = ref(false)

/** 快照态短轮询：后台刷新完成前每 10s 补拉一次（封顶 6 次），拿到新鲜
 *  数据即停——「先显示本地快照，刷新完成后自动覆盖」的覆盖侧。 */
const STALE_POLL_MS = 10_000
const STALE_POLL_MAX = 6
let staleTimer: number | undefined
let stalePolls = 0

function scheduleStalePoll() {
  if (staleTimer !== undefined || stalePolls >= STALE_POLL_MAX) return
  stalePolls += 1
  staleTimer = window.setTimeout(async () => {
    staleTimer = undefined
    await load()
    if (stale.value) scheduleStalePoll()
  }, STALE_POLL_MS)
}

function stopStalePoll() {
  if (staleTimer !== undefined) {
    window.clearTimeout(staleTimer)
    staleTimer = undefined
  }
  stalePolls = 0
}

async function load() {
  try {
    const res = await metadataApi.epicOffers()
    if (res.ok) {
      offers.value = res.offers
      mobile.value = res.mobile
      fetchedAt.value = res.fetchedAt ? res.fetchedAt.slice(11, 16) : ''
      state.value = 'ok'
      stale.value = res.stale === true
      if (stale.value) scheduleStalePoll()
      else stopStalePoll()
    } else if (offers.value.length === 0 && !mobile.value) {
      state.value = 'failed'
    }
  } catch {
    if (offers.value.length === 0 && !mobile.value) state.value = 'failed'
  }
}

/** 'YYYY-M-D'（无前导零）→ 距今的日历日差（UTC 口径，对齐 Epic 窗口日期） */
function daysUntil(day: string): number {
  const [y, m, d] = day.split('-').map(Number)
  if (!y || !m || !d) return 0
  const now = new Date()
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  return Math.round((Date.UTC(y, m - 1, d) - today) / 86_400_000)
}

/** 当期按结束日升序（快结束的在前），预告按开始日升序，预告整段靠后 */
const sorted = computed(() => {
  const live = offers.value
    .filter((o) => !o.upcoming)
    .sort((a, b) => daysUntil(a.end) - daysUntil(b.end))
  const upcoming = offers.value
    .filter((o) => o.upcoming)
    .sort((a, b) => daysUntil(a.start) - daysUntil(b.start))
  return [...live, ...upcoming]
})

/** 状态章文案：当期=剩 N 天，预告=N 天后开始（整句词条，中英语序各自成句） */
function statusText(o: EpicOffer): string {
  if (o.upcoming) {
    const n = daysUntil(o.start)
    if (n <= 0) return t('epicFree.startsToday')
    if (n === 1) return t('epicFree.startsTomorrow')
    return t('epicFree.startsIn', { n })
  }
  const n = daysUntil(o.end)
  if (n <= 0) return t('epicFree.endsToday')
  return t('epicFree.endsIn', { n })
}

/** 移动卡倒计时（GamerPower 源带截止日；breaker 兜底无日期不显示） */
const mobileEndText = computed(() => {
  const end = mobile.value?.end
  if (!end) return ''
  const n = daysUntil(end)
  if (n <= 0) return t('epicFree.endsToday')
  return t('epicFree.endsIn', { n })
})

const HOUR_MS = 3_600_000
let timer: number | undefined

onMounted(() => {
  load()
  timer = window.setInterval(load, HOUR_MS)
})

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
  stopStalePoll()
})
</script>

<template>
  <div class="card epic-free" data-section="dashboard.section.epicFree">
    <div class="epic-free__header">
      <div class="epic-free__title">
        <img src="/assets/logo_epic.ico" alt="" class="epic-free__logo" />
        {{ t('epicFree.title') }}
      </div>
      <div class="epic-free__meta">
        <span v-if="fetchedAt" class="epic-free__time">{{
          t('epicFree.updatedAt', { time: fetchedAt })
        }}</span>
        <span v-if="stale" class="epic-free__refreshing">{{ t('epicFree.refreshing') }}</span>
        <a
          class="epic-free__hub"
          href="https://store.epicgames.com/en-US/free-games"
          target="_blank"
          rel="noopener noreferrer"
        >{{ t('epicFree.goHub') }} →</a>
      </div>
    </div>

    <HlSkeleton
      v-if="state === 'loading'"
      variant="card"
      :count="4"
      class="epic-free__skeleton"
    />
    <HlEmpty
      v-else-if="state === 'failed'"
      size="sm"
      icon=""
      :text="t('epicFree.loadFailed')"
    />
    <HlEmpty
      v-else-if="sorted.length === 0 && !mobile"
      size="sm"
      icon=""
      :text="t('epicFree.empty')"
    />
    <div v-else class="epic-free__grid">
      <a
        v-for="o in sorted"
        :key="`${o.url}-${o.start}`"
        class="epic-card"
        :class="{ 'epic-card--upcoming': o.upcoming }"
        :href="o.url"
        target="_blank"
        rel="noopener noreferrer"
      >
        <div class="epic-card__media">
          <HlImg class="epic-card__img" :src="o.image" :alt="o.titleCn || o.title" loading="lazy" />
          <span class="epic-card__free" :class="{ 'epic-card__free--ghost': o.upcoming }">{{
            t('epicFree.free')
          }}</span>
          <span
            class="epic-card__status"
            :class="{ 'epic-card__status--upcoming': o.upcoming }"
          >
            <span class="epic-card__state">{{ o.upcoming ? t('epicFree.upcoming') : t('epicFree.live') }}</span>
            <span class="epic-card__countdown">{{ statusText(o) }}</span>
          </span>
          <img src="/assets/logo_epic.ico" alt="" class="epic-card__mark" />
        </div>
        <div class="epic-card__body">
          <span class="epic-card__name">{{ o.titleCn || o.title }}</span>
          <!-- 预告卡不出现领取 CTA：右位换成开始日期，与可领取卡一眼区分 -->
          <span v-if="o.upcoming" class="epic-card__start">{{
            t('epicFree.startsOn', { date: o.start })
          }}</span>
          <span v-else class="epic-card__price">
            <span v-if="o.priceOriginal" class="epic-card__orig">{{ o.priceOriginal }}</span>
            <span class="epic-card__claim">{{ t('epicFree.claim') }} ↗</span>
          </span>
        </div>
      </a>

      <!-- 移动端白送：GamerPower 自动源（真名/日期/官方落地链），breaker 立绘兜底 -->
      <a
        v-if="mobile"
        class="epic-card epic-card--mobile"
        :href="mobile.url"
        target="_blank"
        rel="noopener noreferrer"
      >
        <div class="epic-card__media">
          <HlImg class="epic-card__img" :src="mobile.image" :alt="mobile.title || t('epicFree.mobileCard')" loading="lazy" />
          <span class="epic-card__free">{{ t('epicFree.free') }}</span>
          <span class="epic-card__status">
            <span class="epic-card__state epic-card__state--mobile">{{ t('epicFree.mobile') }}</span>
            <span v-if="mobileEndText" class="epic-card__countdown">{{ mobileEndText }}</span>
          </span>
          <img src="/assets/logo_epic.ico" alt="" class="epic-card__mark" />
        </div>
        <div class="epic-card__body">
          <span class="epic-card__name">{{ mobile.title || t('epicFree.mobileCard') }}</span>
          <span class="epic-card__price">
            <span v-if="mobile.worth" class="epic-card__orig">{{ mobile.worth }}</span>
            <span class="epic-card__claim">{{ t('epicFree.claim') }} ↗</span>
          </span>
        </div>
      </a>
    </div>
  </div>
</template>

<style scoped>
.epic-free {
  padding: 18px 20px;
}

.epic-free__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.epic-free__title {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.epic-free__logo {
  display: block;
  width: 18px;
  height: 18px;
}

.epic-free__meta {
  display: flex;
  align-items: center;
  gap: 12px;
}

.epic-free__time {
  font-size: 12px;
  color: var(--text-muted);
}

/* 快照刷新提示：纯文字状态（无动画），accent 色与「更新于」区分 */
.epic-free__refreshing {
  font-size: 12px;
  color: var(--accent);
}

/* 真实外链（Epic 免费游戏总览页），非动作按键 */
.epic-free__hub {
  font-size: 12px;
  color: var(--accent);
  text-decoration: none;
}

.epic-free__hub:hover {
  text-decoration: underline;
}

.epic-free__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 12px;
}

.epic-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--bg-soft);
  text-decoration: none;
  transition:
    transform var(--transition),
    border-color var(--transition),
    box-shadow var(--transition);
}

.epic-card:hover {
  transform: translateY(-2px);
  border-color: var(--accent);
  box-shadow: var(--shadow-sm);
}

.epic-card__media {
  position: relative;
  aspect-ratio: 16 / 9;
  background: var(--bg-soft);
}

.epic-card__img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* 三段平滑沉底：与仪表盘轮播同一手法，状态章落在最暗处 */
.epic-card__media::after {
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

.epic-card__free {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 1;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--success);
  color: var(--text-on-fill);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
}

/* 预告的免费章改描边空心态：实心绿=现在能领，空心=还没轮到 */
.epic-card__free--ghost {
  background: transparent;
  border: 1px solid var(--success-on-dark);
  color: var(--success-on-dark);
}

.epic-card__status {
  position: absolute;
  bottom: 6px;
  left: 8px;
  right: 8px;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.epic-card__state {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--success-a15);
  color: var(--success-on-dark);
  font-size: 10px;
  font-weight: 600;
}

/* 预告章换中性蓝（未开始的语义），倒计时跟随其后 */
.epic-card__status--upcoming .epic-card__state {
  background: var(--accent-a15);
  color: var(--accent);
}

/* 移动端章：来源语义（不是时间状态），沿用品牌蓝 */
.epic-card__state--mobile {
  background: var(--accent-a15);
  color: var(--accent);
}

.epic-card__countdown {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-on-fill);
  text-shadow: 0 1px 4px rgba(0, 0, 0, 0.55);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 商店来源标：每张卡右上角一枚 Epic logo */
.epic-card__mark {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 1;
  width: 16px;
  height: 16px;
  border-radius: 4px;
  background: var(--bg-card);
  padding: 2px;
  box-shadow: var(--shadow-sm);
}

.epic-card__body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px 10px;
}

.epic-card__name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.epic-card__price {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.epic-card__orig {
  font-size: 11px;
  color: var(--text-muted);
  text-decoration: line-through;
}

.epic-card__claim {
  font-size: 11px;
  font-weight: 600;
  color: var(--success-on-dark);
}

/* ─── 预告卡（不可领取）刻意异形：虚线框 + 封面去饱和 + 无领取 CTA ─── */
.epic-card--upcoming {
  border-style: dashed;
  background: transparent;
}

.epic-card--upcoming:hover {
  transform: none;
  box-shadow: none;
}

.epic-card--upcoming .epic-card__img {
  filter: saturate(0.5) brightness(0.85);
}

.epic-card--upcoming .epic-card__body {
  padding-top: 7px;
  padding-bottom: 8px;
}

.epic-card--upcoming .epic-card__name {
  font-weight: 500;
}

.epic-card__start {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--text-muted);
}
</style>
