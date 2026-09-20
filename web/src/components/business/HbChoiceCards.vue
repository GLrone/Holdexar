<script setup lang="ts">
/**
 * HB 当月包卡片 —— Humble Choice 本月内容一览，仪表盘分节卡。
 *
 * 数据链：GET /metadata/hb/offers（纯本地库读：记账游标 → 当月标签 →
 * games.is_hb 标记行 + 国区价）→ 挂载即拉 + 每小时轻刷（抓取链每日跑，
 * 库内标记到位后卡片自动浮出）。游戏卡点击进站内详情页；头部「跳过本月 /
 * 前往月包」是官方页直达外链。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { metadataApi, type HbChoiceGame } from '@/api/client'
import { formatCnyFen } from '@/api/regions'
import { useI18n } from '@/locales'
import { HlEmpty, HlImg, HlSkeleton } from '@/components/ui'

const router = useRouter()
const { t } = useI18n()

const games = ref<HbChoiceGame[]>([])
const label = ref('')
const monthUrl = ref('')
const skipUrl = ref('')
/** loading=首次拉取中 / ok=有数据 / failed=无数据可显（尚未入库或拉取失败） */
const state = ref<'loading' | 'ok' | 'failed'>('loading')

async function load() {
  try {
    const res = await metadataApi.hbChoiceOffers()
    if (res.ok && res.games.length > 0) {
      games.value = res.games
      label.value = res.label
      monthUrl.value = res.monthUrl
      skipUrl.value = res.skipUrl
      state.value = 'ok'
    } else if (games.value.length === 0) {
      state.value = 'failed'
    }
  } catch {
    if (games.value.length === 0) state.value = 'failed'
  }
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
        :key="g.appid"
        class="hb-card"
        :title="g.name"
        @click="router.push(`/game/${g.appid}`)"
      >
        <div class="hb-card__media">
          <HlImg class="hb-card__img" :src="g.headerImage" :alt="g.name" loading="lazy" />
          <span v-if="g.discount > 0" class="hb-card__off">-{{ g.discount }}%</span>
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
            <span v-else class="hb-card__now hb-card__now--none">—</span>
          </span>
        </div>
      </div>
    </div>
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
