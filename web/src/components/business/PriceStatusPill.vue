<script setup lang="ts">
/**
 * 顶栏价格更新状态胶囊：一个胶囊只表达一个产品结论——正在更新 / 价格已更新 /
 * 部分更新 / 等待更新。
 *
 * 数据来源固定为两处：crawlStatus store（SSE 的运行数据）与「是否有过跑完的
 * 任务」。不新建状态体系，也不把内部 job / 队列 / 速度 / 代理放进主状态与
 * 普通 tooltip（技术诊断入口另说）。
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

const status = computed(() =>
  priceStatusOf({
    running: crawl.running,
    done: crawl.done,
    fail: crawl.fail,
    total: crawl.total,
    lastStatus: lastJobStatus.value,
    hasHistory: hasHistory.value || sawActivity.value,
  }),
)

const label = computed(() => {
  const s = status.value
  switch (s.kind) {
    case 'running':
      return s.total > 0
        ? t('header.crawlRunning', { done: s.done, total: s.total })
        : t('header.crawlRunningBare')
    case 'partial':
      return t('header.crawlPartial', { done: s.done, total: s.total })
    case 'done':
      return t('header.crawlDone', { done: s.done, total: s.total })
    case 'idle':
      return t('header.crawlIdle')
    default:
      return t('header.crawl')
  }
})

/** 颜色只表达状态：进行中=强调色，部分=待处理色，已更新=正常色，未更新=弱化 */
const color = computed(() => {
  switch (status.value.kind) {
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

/** 悬停补充信息：只补结果与影响（数量 / 稍后自动重试），不补实现细节 */
const tip = computed(() => {
  const s = status.value
  if (s.kind === 'running') return ''
  if (s.kind === 'partial') {
    return s.retry > 0
      ? t('header.crawlTip', { done: s.done, fail: s.retry })
      : t('header.crawlTipDone', { done: s.done })
  }
  if (s.kind === 'done') return t('header.crawlTipDone', { done: s.done })
  return ''
})
</script>

<template>
  <div class="header-pill" :title="tip">
    <HlIcon name="refresh" />
    <span :style="{ color }">{{ label }}</span>
  </div>
</template>