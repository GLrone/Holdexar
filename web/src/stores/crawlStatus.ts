import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { crawlApi } from '@/api/client'
import { CYCLE_ACTIVE } from '@/lib/headerStatus'

import { nextPriceCycle, type PriceCycleMark } from '@/lib/priceRefresh'

/** 爬取状态（SSE 驱动）：顶栏胶囊 + 爬取任务页共用。 */
export const useCrawlStatusStore = defineStore('crawlStatus', () => {
  const running = ref(false)
  const done = ref(0)
  const ok = ref(0)
  const fail = ref(0)
  const qsize = ref(0)
  const speed = ref(0)
  /* 任务目标量：后端在任务启动时定死（初始任务数），随进度事件下发。
     不能用 done+qsize 拼总数——队列排干/逐层入队/重推都会让它波动。 */
  const total = ref(0)
  const activeJobId = ref<number | null>(null)
  const lastEventAt = ref<string>('')

  /* 最近一次收敛的价格刷新周期（SSE price_cycle.completed）：库视图据此失效
     列表缓存并原地重拉。按 cycleId 去重——同一轮重复到达不触发第二次请求。 */
  const priceCycle = ref<PriceCycleMark | null>(null)

  /* 一轮价格刷新由多个任务段串行组成；进度按轮累计（段与段之间不清零），
     岛上的进度条与百分比在轮内单调推进。轮次未激活时（手动抓取、修复等
     独立任务）进度退回当前任务段的计数口径。 */
  const roundDone = ref(0)
  const roundTotal = ref(0)
  const roundTracking = ref(false)

  /** 轮内累计进度（含在跑任务段的实时计数）；null = 当前无轮次口径 */
  const roundProgress = computed(() => {
    if (!roundTracking.value) return null
    const liveDone = running.value ? done.value : 0
    const liveTotal = running.value ? total.value : 0
    return { done: roundDone.value + liveDone, total: roundTotal.value + liveTotal }
  })

  /* 以最近一轮的记账状态核对轮次是否在跑，并把该轮已收尾任务段的完成量补进
     累计（页面半途打开时，前几段的量从这里补齐）。查询失败保持现状。 */
  async function syncRoundScope() {
    try {
      const [cycle] = await crawlApi.cycles(1)
      if (!cycle || !CYCLE_ACTIVE.has(cycle.status)) {
        roundTracking.value = false
        return
      }
      const jobs = await crawlApi.jobs(10)
      let finishedDone = 0
      let finishedTotal = 0
      for (const job of jobs) {
        if (job.cycleId !== cycle.id) continue
        finishedDone += job.stats?.processed ?? 0
        finishedTotal += job.stats?.total ?? 0
      }
      roundDone.value = finishedDone
      roundTotal.value = finishedTotal
      roundTracking.value = true
    } catch {
      /* 拉不到轮次不改现有展示 */
    }
  }

  let source: EventSource | null = null
  let started = false

  function start() {
    if (started) return
    started = true
    source = new EventSource('/api/v1/events/stream')

    source.addEventListener('crawl.progress', (e) => {
      running.value = true
      const data = JSON.parse((e as MessageEvent).data)
      done.value = data.done ?? 0
      ok.value = data.ok ?? 0
      fail.value = data.fail ?? 0
      qsize.value = data.qsize ?? 0
      speed.value = data.speed ?? 0
      total.value = data.total ?? done.value + qsize.value
      lastEventAt.value = new Date().toLocaleTimeString()
    })

    source.addEventListener('job.started', (e) => {
      const data = JSON.parse((e as MessageEvent).data)
      activeJobId.value = data.job_id ?? null
      running.value = true
      done.value = 0
      ok.value = 0
      fail.value = 0
      total.value = 0
      void syncRoundScope()
    })

    source.addEventListener('job.status', (e) => {
      const data = JSON.parse((e as MessageEvent).data)
      if (['done', 'failed', 'stopped'].includes(data.status)) {
        /* 任务段收尾并入轮次累计；轮内下一段起算时不清零 */
        if (roundTracking.value) {
          roundDone.value += done.value
          roundTotal.value += total.value
        }
        running.value = false
        activeJobId.value = null
      }
    })

    /* 价格周期收敛（completed / partial / failed / cancelled）。判定与去重在
       lib/priceRefresh：同一轮重复到达不推进，页面不会重复请求。 */
    source.addEventListener('price_cycle.completed', (e) => {
      const next = nextPriceCycle(priceCycle.value, JSON.parse((e as MessageEvent).data))
      if (next) {
        priceCycle.value = next
        roundTracking.value = false
        roundDone.value = 0
        roundTotal.value = 0
      }
    })

    source.onerror = () => {
      // EventSource 自动重连
    }

    void syncRoundScope()
  }

  return {
    running,
    done,
    ok,
    fail,
    qsize,
    speed,
    total,
    activeJobId,
    lastEventAt,
    priceCycle,
    roundProgress,
    start,
  }
})
