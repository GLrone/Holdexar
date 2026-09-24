<script setup lang="ts">
/**
 * 顶栏价格更新状态胶囊：一个胶囊只表达一个产品结论——正在更新 / 价格已更新 /
 * 部分更新 / 等待更新。
 *
 * 数据来源固定为两处：crawlStatus store（SSE 的运行数据）与「是否有过跑完的
 * 任务」。store 计数的口径是任务批次（1 批 = 1 地区 × ≤400 款），不是游戏数，
 * 因此主状态与 tooltip 都不展示数量。不新建状态体系，也不把内部 job / 队列 /
 * 速度 / 代理放进主状态与普通 tooltip（技术诊断入口另说）。
 */
import { computed, onMounted, ref, watch } from 'vue'

import { crawlApi } from '@/api/client'
import { priceStatusOf } from '@/lib/headerStatus'
import { useI18n } from '@/locales'
import { useCrawlStatusStore } from '@/stores/crawlStatus'
import { HlIcon } from '@/components/ui'

const crawl = useCrawlStatusStore()
const { t } = useI18n()

/** 有过跑完的任务 = 已经更新过；用于区分「价格已更新」与从未更新的「等待更新」 */
const hasHistory = ref(false)
/** 本会话内出现过更新活动：任务结束后不得回落成「等待更新」 */
const sawActivity = ref(false)
/** 最近一次任务的终态：区分「跑完」与「没跑完就停了」（后者算部分更新） */
const lastJobStatus = ref<string | null>(null)

watch(
  () => crawl.running,
  async (running, wasRunning) => {
    if (running) {
      sawActivity.value = true
      lastJobStatus.value = null
      return
    }
    if (!wasRunning) return
    try {
      lastJobStatus.value = (await crawlApi.jobs(1))[0]?.status ?? null
    } catch {
      lastJobStatus.value = null
    }
  },
)

onMounted(async () => {
  try {
    hasHistory.value = (await crawlApi.jobs(1))[0]?.status === 'done'
  } catch {
    /* 读不到按「等待更新」显示，不打断首屏 */
  }
})

const kind = computed(() =>
  priceStatusOf({
    running: crawl.running,
    ok: crawl.ok,
    fail: crawl.fail,
    total: crawl.total,
    lastStatus: lastJobStatus.value,
    hasHistory: hasHistory.value || sawActivity.value,
  }),
)

const label = computed(() => {
  switch (kind.value) {
    case 'running':
      return t('header.crawlRunning')
    case 'partial':
      return t('header.crawlPartial')
    case 'done':
    case 'idle':
      return t('header.crawlDone')
    case 'waiting':
      return t('header.crawl')
    default:
      return t('header.crawl')
  }
})

/** 颜色只表达状态：进行中=强调色，部分=待处理色，已更新=正常色，未更新=弱化 */
const color = computed(() => {
  switch (kind.value) {
    case 'running':
      return 'var(--accent)'
    case 'partial':
      return 'var(--warning)'
    case 'done':
    case 'idle':
      return 'var(--success)'
    default:
      return 'var(--text-muted)'
  }
})

/** 悬停补充信息：只补结论与出路，不带数量（内部计数是批次口径，非游戏数） */
const tip = computed(() => {
  switch (kind.value) {
    case 'partial':
      return t('header.crawlTip')
    case 'done':
    case 'idle':
      return t('header.crawlTipDone')
    default:
      return ''
  }
})
</script>

<template>
  <div class="header-pill" :title="tip">
    <HlIcon name="refresh" />
    <span :style="{ color }">{{ label }}</span>
  </div>
</template>
