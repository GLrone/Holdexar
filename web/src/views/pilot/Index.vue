<script setup lang="ts">
import { computed, ref } from 'vue'

import { useI18n } from '@/locales'
import HlButton from '@/components/ui/HlButton.vue'
import HlPilotConsole from '@/components/business/HlPilotConsole.vue'
import HlPilotCards from '@/components/business/HlPilotCards.vue'
import HlPilotThinking from '@/components/business/HlPilotThinking.vue'
import { usePilotStore } from '@/stores/pilot'
import type { PilotFacts } from '@/api/client'

/**
 * 领航台整页（放大版对话）：与抽屉共用 HlPilotConsole（同一实现、同一会话
 * 账本——抽屉里聊一半来这里接着聊）。下挂组件总览：领航员能产出的全部界面
 * 组件以夹具数据真实渲染，用户不触发问答也能直接看全每个组件的形态。
 */
const { t } = useI18n()
const pilotStore = usePilotStore()

const galleryOpen = ref(false)

const galleryCards: PilotFacts[] = [
  {
    kind: 'price',
    appid: 292030,
    name: 'The Witcher 3: Wild Hunt',
    positiveRate: 0.97,
    reviewCount: 660000,
    cn: { cnyFen: 6200, discount: 0 },
    lowest: { cnyFen: 3100, snapshotAt: '2026-05-11T00:00:00' },
    year: { minFen: 3100, maxFen: 12700, medianFen: 6200, count: 48 },
  },
  {
    kind: 'games',
    items: [
      { appid: 413150, name: 'Stardew Valley', cnyFen: 4800, discount: 0, positiveRate: 0.98, reviewCount: 900000 },
      { appid: 1245620, name: 'Dave the Diver', cnyFen: 9900, discount: 10, positiveRate: 0.96, reviewCount: 60000 },
      { appid: 1145360, name: 'Hades', cnyFen: 8800, discount: 0, positiveRate: 0.98, reviewCount: 300000 },
    ],
  },
  {
    // 榜单/清单形态：卡题 + 行内注记（热销榜、降价事件、关注来源共用）
    kind: 'games',
    titleKey: 'drops',
    total: 17,
    items: [
      {
        appid: 2680010, name: 'The First Berserker: Khazan', cnyFen: null, discount: 40,
        positiveRate: 0.91, reviewCount: 30000, note: { key: 'ev_new_historical_low', v: 'AR', at: '2026-10-04T15:27:48' },
      },
      {
        appid: 1245620, name: 'Dave the Diver', cnyFen: 9900, discount: 10,
        positiveRate: 0.96, reviewCount: 60000, note: { key: 'ev_price_drop', v: 'CN', at: '2026-10-04T09:12:00' },
      },
    ],
  },
  {
    kind: 'family',
    bound: true,
    joined: true,
    walletRegion: 'CN',
    total: 3,
    members: [
      { steamid: '76561198000000001', name: 'Wuruo_CN', role: 'primary', region: 'CN' },
      { steamid: '76561198000000002', name: 'Wuruo_US', role: 'member', region: 'US' },
    ],
  },
  {
    kind: 'achievements',
    hasCredential: true,
    platinum: 12,
    unlocked: 29122,
    total: 58400,
    completionRate: 49.9,
    platinums: [
      { appid: 413150, name: 'Stardew Valley' },
      { appid: 1145360, name: 'Hades' },
      { appid: 292030, name: 'The Witcher 3: Wild Hunt' },
    ],
    recent: [
      { appid: 1245620, name: 'Ecologist', gameName: 'Dave the Diver', at: 1759500000 },
      { appid: 413150, name: 'Philanthropist', gameName: 'Stardew Valley', at: 1759400000 },
    ],
  },
  {
    // 删除提议：封面 + 名称 + 原因（不在目录的游戏凭 appid 拼 Steam 头图）
    kind: 'proposal',
    pid: 'demo-del',
    action: 'delete',
    state: 'pending',
    args: {},
    items: [
      { key: 'alert:3', appid: 2680010, name: 'The First Berserker: Khazan', reason: 'removed' },
      { key: 'alert:4', appid: 1794880, name: 'AppID 1794880', reason: 'noGame' },
      { key: 'bill_import:9', name: 'Steam bills 2026-09', reason: 'dupImport' },
    ],
  },
  { kind: 'action', action: 'monitor_add', appid: 413150, name: 'Stardew Valley', state: 'active' },
  { kind: 'navigate', target: 'alerts', path: '/alerts' },
  {
    kind: 'rows',
    titleKey: 'diagnosis',
    rows: [
      { k: '', vKey: 'coverage', data: { ok: 14, total: 17 }, tone: 'warn' },
      { k: 'RU', vKey: 'failed', v: 'locked', tone: 'bad', at: '2026-10-03T14:00:00' },
      { k: 'TR', vKey: 'notAttempted', tone: 'warn' },
    ],
  },
]

const galleryThink = computed(() => t('pilot.gallery.thinkDemo'))
</script>

<template>
  <section class="pilot-page">
    <!-- 会话列挂点：占住居中对话列之外的左侧空白区（Console Teleport 进来），
         独立于思考链/输入框的内容列；窄屏隐藏（Console 回落顶部标签栏） -->
    <div class="pilot-page__side">
      <div id="pilot-side-target" class="pilot-page__side-inner"></div>
    </div>

    <div class="pilot-page__main">
      <div class="pilot-page__console">
        <HlPilotConsole :game="pilotStore.game" page />
      </div>

      <section class="pilot-page__gallery">
        <header class="pilot-gallery__head">
          <div class="pilot-gallery__heading">
            <span class="pilot-gallery__title">{{ t('pilot.gallery.title') }}</span>
            <span class="pilot-gallery__desc">{{ t('pilot.gallery.desc') }}</span>
          </div>
          <HlButton variant="text" @click="galleryOpen = !galleryOpen">
            {{ galleryOpen ? t('pilot.gallery.hide') : t('pilot.gallery.show') }}
          </HlButton>
        </header>

        <div v-if="galleryOpen" class="pilot-gallery__body">
          <div class="pilot-gallery__cards">
            <HlPilotCards :cards="galleryCards" />
          </div>
          <div class="pilot-gallery__think">
            <HlPilotThinking :text="galleryThink" :dur-sec="6" />
          </div>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.pilot-page {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  width: 100%;
  padding: 16px 18px 40px;
}

/* 会话列：钉在内容区左缘（sticky 跟随滚动），定高让内滚生效 */
.pilot-page__side {
  flex: none;
  width: 236px;
  position: sticky;
  top: 12px;
  display: flex;
  height: calc(100vh - 84px);
  min-width: 0;
}

.pilot-page__side-inner {
  flex: 1;
  min-width: 0;
  display: flex;
}

/* 对话主列：在剩余空间内保持原版心（居中、压长行） */
.pilot-page__main {
  flex: 1;
  min-width: 0;
  max-width: 1200px;
  margin-inline: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 整页对话区：turns 不自滚（不出内层滚动条），随页流动由页面滚动条负责；
   console 运行时按「能否滚」解析有效滚动容器，钉底锚在输入区 */
.pilot-page__console :deep(.pilot-turns) {
  max-height: none;
  min-height: 320px;
  overflow: visible;
}

/* 窄屏：会话列退出（Console 内部回落顶部标签栏） */
@media (max-width: 1080px) {
  .pilot-page__side {
    display: none;
  }
}

.pilot-page__gallery {
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  background: var(--bg-card);
  padding: 12px 16px;
}

.pilot-gallery__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.pilot-gallery__heading {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.pilot-gallery__title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.pilot-gallery__desc {
  font-size: 12px;
  color: var(--text-secondary);
}

.pilot-gallery__body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 12px;
}

.pilot-gallery__think {
  max-width: 640px;
}
</style>
