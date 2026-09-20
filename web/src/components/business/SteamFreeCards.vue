<script setup lang="ts">
/**
 * Steam 喜加一卡片组 —— 正在赠送中的限时免费（free_to_keep），仪表盘分节卡。
 *
 * 数据链：GET /metadata/steam/offers（纯本地库零外网：games.free_kind='promo'
 * 由写库层随每轮爬取维护，promo_end_at = Steam free_to_keep_ends 精确到秒）
 * → 前端挂载即拉 + 10 分钟轮询。
 * 条件渲染契约：**无正在赠送的游戏时整块不渲染**（外壳也不出）——与 Epic
 * 模块的「空态占位」刻意不同：赠送是稀缺事件，模块出现本身就是信号。
 * 卡片直达站内游戏详情（免费态价格区/走势均在详情页），不外链商店页。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { metadataApi, type SteamFreeOffer } from '@/api/client'
import { useI18n } from '@/locales'
import { HlImg } from '@/components/ui'

const { t } = useI18n()

const offers = ref<SteamFreeOffer[]>([])
const fetchedAt = ref('')
/** true = 至少拉到一次响应（区别于「还没拉过」）：拉过但空 = 无赠送，隐藏 */
const loaded = ref(false)

const visible = computed(() => loaded.value && offers.value.length > 0)

const POLL_MS = 10 * 60 * 1000
let timer: number | undefined

async function load() {
  try {
    const res = await metadataApi.steamFreeOffers()
    if (res.ok) {
      offers.value = res.offers
      fetchedAt.value = res.fetchedAt ? res.fetchedAt.slice(11, 16) : ''
      loaded.value = true
    }
  } catch {
    // 拉取失败保留上一份数据；从未成功过则维持隐藏（模块出现即信号的另一面：
    // 不能因为一次网络抖动闪出空模块）
  }
}

/** 精确到秒的结束时间戳 → 倒计时整句（当天=今天结束，24h 内=小时，其余=天） */
function countdownText(endTs: number): string {
  const ms = endTs * 1000 - Date.now()
  if (ms <= 0) return t('steamFree.endsToday')
  const hours = Math.floor(ms / 3_600_000)
  if (hours < 24) return t('steamFree.endsInHours', { n: Math.max(hours, 1) })
  return t('steamFree.endsInDays', { n: Math.floor(ms / 86_400_000) })
}

/** 结束日期文案（超出 48h 才显示日期，倒计时短语已足够） */
function endDateText(endTs: number): string {
  const d = new Date(endTs * 1000)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** 按结束时刻升序（快结束的在前） */
const sorted = computed(() => [...offers.value].sort((a, b) => a.endTs - b.endTs))

onMounted(() => {
  load()
  timer = window.setInterval(load, POLL_MS)
})

onBeforeUnmount(() => {
  if (timer !== undefined) window.clearInterval(timer)
})
</script>

<template>
  <div v-if="visible" class="card steam-free" data-section="dashboard.section.steamFree">
    <div class="steam-free__header">
      <div class="steam-free__title">
        <img src="/assets/logo_steam.png" alt="" class="steam-free__logo" />
        {{ t('steamFree.title') }}
      </div>
      <span v-if="fetchedAt" class="steam-free__time">{{
        t('steamFree.updatedAt', { time: fetchedAt })
      }}</span>
    </div>

    <div class="steam-free__grid">
      <router-link
        v-for="o in sorted"
        :key="o.appid"
        class="steam-card"
        :to="`/game/${o.appid}`"
      >
        <div class="steam-card__media">
          <HlImg class="steam-card__img" :src="o.headerImage || ''" :alt="o.name" loading="lazy" />
          <span class="steam-card__free">{{ t('steamFree.free') }}</span>
          <span class="steam-card__status">
            <span class="steam-card__state">{{ t('steamFree.live') }}</span>
            <span class="steam-card__countdown">{{ countdownText(o.endTs) }}</span>
          </span>
          <img src="/assets/logo_steam.png" alt="" class="steam-card__mark" />
        </div>
        <div class="steam-card__body">
          <span class="steam-card__name">{{ o.name }}</span>
          <span class="steam-card__price">
            <span v-if="o.originalPriceFen" class="steam-card__orig">¥{{ (o.originalPriceFen / 100).toFixed(0) }}</span>
            <span class="steam-card__end">{{ endDateText(o.endTs) }}</span>
          </span>
        </div>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.steam-free {
  padding: 18px 20px;
}

.steam-free__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.steam-free__title {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.steam-free__logo {
  display: block;
  width: 18px;
  height: 18px;
}

.steam-free__time {
  font-size: 12px;
  color: var(--text-muted);
}

.steam-free__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 12px;
}

.steam-card {
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

.steam-card:hover {
  transform: translateY(-2px);
  border-color: var(--accent);
  box-shadow: var(--shadow-sm);
}

.steam-card__media {
  position: relative;
  aspect-ratio: 16 / 9;
  background: var(--bg-soft);
}

.steam-card__img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* 三段平滑沉底：与 Epic 卡同一手法，状态章落在最暗处 */
.steam-card__media::after {
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

.steam-card__free {
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

.steam-card__status {
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

.steam-card__state {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--success-a15);
  color: var(--success-on-dark);
  font-size: 10px;
  font-weight: 600;
}

.steam-card__countdown {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-on-fill);
  text-shadow: 0 1px 4px rgba(0, 0, 0, 0.55);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.steam-card__mark {
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

.steam-card__body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px 10px;
}

.steam-card__name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.steam-card__price {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.steam-card__orig {
  font-size: 11px;
  color: var(--text-muted);
  text-decoration: line-through;
}

.steam-card__end {
  font-size: 11px;
  color: var(--text-muted);
}
</style>
