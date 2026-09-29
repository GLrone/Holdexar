/* ════════════════════════════════════════════════════════════════════
   usePriceStatus.ts — 价格更新状态（灵动岛空闲态与它展开面的数据源）
   一个结论只表达一件事：正在更新 / 价格已更新 / 更新未完成 / 等待更新 /
   价格未更新。

   结论以**刷新轮次**为准：最近一轮的终态决定这轮收敛没有，最近一次真正写入
   价格的轮次收尾时刻决定数据有多旧。store 只补运行中的实时计数。
   轮次与批次的计数口径都不是游戏数，故对外不给任何数量。

   空闲结论只在数据已旧（stale）时占岛：距上次成功写入未超出静默窗口就保持
   静默，岛体让位给消息与任务；从未写入过视同已旧。
   ════════════════════════════════════════════════════════════════════ */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { crawlApi, type PriceCycleItem } from '@/api/client'
import { priceStatusOf } from '@/lib/headerStatus'
import { useI18n } from '@/locales'
import { useCrawlStatusStore } from '@/stores/crawlStatus'

/** 扫描的轮次条数：够越过近期连续未收敛的轮次，回看到最近一次真正写入的轮次 */
const CYCLE_SCAN = 20

/** 数据静默窗口：距上次成功写入不超过该时长即视为新鲜，空闲结论不占岛 */
const FRESH_WINDOW_MS = 6 * 60 * 60 * 1000

/** 相对时长的重算间隔：分钟档要跟着走，不能让「N 小时前」停在旧值 */
const AGO_TICK_MS = 60_000

/** 轮次快照的重拉间隔：收尾有事件驱动重读，这里兜的是 SSE 断线丢事件的底 */
const CYCLE_RESYNC_MS = 5 * 60_000

export function usePriceStatus() {
  const crawl = useCrawlStatusStore()
  const { t } = useI18n()

  /** 最近一轮刷新周期；null = 还没有任何轮次记录 */
  const latestCycle = ref<PriceCycleItem | null>(null)
  /** 最近一次真正写入价格的轮次收尾时刻（ISO）；null = 从未写入过 */
  const lastUpdatedAt = ref<string | null>(null)
  /** 本会话内出现过更新活动：轮次收尾前不得回落成「等待更新」 */
  const sawActivity = ref(false)
  /** 相对时长的计算基准时刻 */
  const now = ref(Date.now())

  let ticker: number | undefined
  let resyncTicker: number | undefined

  async function loadCycles() {
    try {
      const cycles = await crawlApi.cycles(CYCLE_SCAN)
      latestCycle.value = cycles[0] ?? null
      lastUpdatedAt.value = cycles.find((c) => (c.stats?.unitsOk ?? 0) > 0)?.finishedAt ?? null
    } catch {
      /* 读不到就保持上一次的结论：不把状态改写成比实际更好或更坏 */
    }
  }

  /** 轮次收尾紧随任务结束，故在运行结束这一刻重读一次，不轮询 */
  watch(
    () => crawl.running,
    (running) => {
      if (running) {
        sawActivity.value = true
        return
      }
      void loadCycles()
    },
  )

  onMounted(() => {
    void loadCycles()
    ticker = window.setInterval(() => {
      now.value = Date.now()
    }, AGO_TICK_MS)
    resyncTicker = window.setInterval(() => {
      void loadCycles()
    }, CYCLE_RESYNC_MS)
  })

  onBeforeUnmount(() => {
    if (ticker !== undefined) window.clearInterval(ticker)
    if (resyncTicker !== undefined) window.clearInterval(resyncTicker)
  })

  const hasHistory = computed(() => sawActivity.value || lastUpdatedAt.value !== null)

  /** 数据是否已旧：从未写入过，或距上次成功写入已超出静默窗口 */
  const stale = computed(() => {
    const at = lastUpdatedAt.value
    if (!at) return true
    const ms = now.value - new Date(at).getTime()
    return !Number.isFinite(ms) || ms > FRESH_WINDOW_MS
  })

  const kind = computed(() =>
    priceStatusOf({
      running: crawl.running,
      ok: crawl.ok,
      fail: crawl.fail,
      total: crawl.total,
      lastStatus: latestCycle.value?.status ?? null,
      hasHistory: hasHistory.value,
    }),
  )

  const label = computed(() => {
    switch (kind.value) {
      case 'running':
        return t('island.price.running')
      case 'partial':
        return stale.value ? t('island.price.stale') : t('island.price.partial')
      case 'done':
      case 'idle':
        return stale.value ? t('island.price.stale') : t('island.price.done')
      default:
        return t('island.price.waiting')
    }
  })

  /** 颜色只表达状态：进行中=强调色，数据已旧/未收敛=待处理色，已更新=正常色，未写入=弱化 */
  const color = computed(() => {
    switch (kind.value) {
      case 'running':
        return 'var(--accent)'
      case 'partial':
        return 'var(--warning)'
      case 'done':
      case 'idle':
        return stale.value ? 'var(--warning)' : 'var(--success)'
      default:
        return 'var(--text-muted)'
    }
  })

  /** 悬停补充信息：只补结论与出路，不带数量（内部计数是批次口径，非游戏数） */
  const tip = computed(() => {
    switch (kind.value) {
      case 'partial':
        return t('island.price.tip')
      case 'done':
      case 'idle':
        return stale.value ? '' : t('island.price.tipDone')
      default:
        return ''
    }
  })

  /** 数据距今多久：分 / 时 / 天三档，超过一天不再细分 */
  const ago = computed(() => {
    const at = lastUpdatedAt.value
    if (!at) return ''
    const ms = now.value - new Date(at).getTime()
    if (!Number.isFinite(ms) || ms < 0) return ''
    const minutes = Math.floor(ms / 60_000)
    if (minutes < 1) return t('island.ago.justNow')
    if (minutes < 60) return t('island.ago.minutes', { n: minutes })
    const hours = Math.floor(minutes / 60)
    if (hours < 24) return t('island.ago.hours', { n: hours })
    return t('island.ago.days', { n: Math.floor(hours / 24) })
  })

  /** 展开态正文：数据有多旧。给不出写入时刻时留空，岛体随之收短 */
  const detail = computed(() => (ago.value ? t('island.price.updatedAt', { time: ago.value }) : ''))

  return { kind, label, color, tip, detail, stale }
}
