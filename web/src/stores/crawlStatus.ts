import { defineStore } from 'pinia'
import { ref } from 'vue'

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
    })

    source.addEventListener('job.status', (e) => {
      const data = JSON.parse((e as MessageEvent).data)
      if (['done', 'failed', 'stopped'].includes(data.status)) {
        running.value = false
        activeJobId.value = null
      }
    })

    /* 价格周期收敛（completed / partial / failed / cancelled）。判定与去重在
       lib/priceRefresh：同一轮重复到达不推进，页面不会重复请求。 */
    source.addEventListener('price_cycle.completed', (e) => {
      const next = nextPriceCycle(priceCycle.value, JSON.parse((e as MessageEvent).data))
      if (next) priceCycle.value = next
    })

    source.onerror = () => {
      // EventSource 自动重连
    }
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
    start,
  }
})
