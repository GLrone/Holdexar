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

  /** 轮内累计进度（含在跑任务段的实时计数）；null = 当前无轮次口径。
      分母不叠加在跑段 total：roundTotal（冻结轮批次总账与已启动段精确值
      的较大者）已含在跑段，再叠加会让分母逐段跳大。max 兜 REST 对账
      未落地的窗口——此时以「分子已完成 + 在跑段 total」保底，不重复计数。 */
  const roundProgress = computed(() => {
    if (!roundTracking.value) return null
    const liveDone = running.value ? done.value : 0
    const liveTotal = running.value ? total.value : 0
    return {
      done: roundDone.value + liveDone,
      total: Math.max(roundTotal.value, roundDone.value + liveTotal),
    }
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
      /* 30：轮内段数 ≤4 之外再留同窗余量——jobs 按 id 降序截断，秋促长轮
         期间修复/手动任务会把轮内早段挤出小窗口，分子分母随之缺段 */
      const jobs = await crawlApi.jobs(30)
      let finishedDone = 0
      let startedTotal = 0
      for (const job of jobs) {
        if (job.cycleId !== cycle.id) continue
        finishedDone += job.stats?.processed ?? 0
        startedTotal += job.stats?.total ?? 0
      }
      roundDone.value = finishedDone
      /* 分母 = 建轮冻结的轮批次总账与已启动段精确 count 的较大者：冻结预估
         不含补抓欠账段、批量切分也可能比预估细，启动后以精确值兜底——两者
         都齐了总数不再随段启动跳变，「总队列」从建轮起可见 */
      roundTotal.value = Math.max(cycle.batchesExpected ?? 0, startedTotal)
      roundTracking.value = true
    } catch {
      /* 拉不到轮次不改现有展示 */
    }
  }

  let source: EventSource | null = null
  let started = false

  /** SSE 断线重连后事件可能有缺口（收尾事件丢失会让「进行中」标志卡住）：
      连接建立即以执行占用的 REST 账本对账；轮次快照的重读由 running 的
      watch 链与消费方的定时兜底完成。 */
  async function resyncRunning() {
    try {
      const a = await crawlApi.active()
      running.value = a.busy === true || a.activeJobId !== null
    } catch {
      /* 拉不到占用账本，保持现状 */
    }
  }

  function start() {
    if (started) return
    started = true
    source = new EventSource('/api/v1/events/stream')

    source.onopen = () => {
      void resyncRunning()
      /* 断线重连会丢轮内事件：以 REST 账本对账轮次追踪，避免退回段级
         口径让「总队列」随段跳变 */
      void syncRoundScope()
    }

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
        /* 分子并入轮次累计（轮内下一段起算时不清零）；分母不加——本段
           total 在启动时已随 syncRoundScope 的 startedTotal 进入
           max(batchesExpected, startedTotal) 分母，收尾再加会让分母跳大、
           进度条倒退。 */
        if (roundTracking.value) {
          roundDone.value += done.value
        }
        running.value = false
        activeJobId.value = null
      }
    })

    /* 生产爬取入口空闲：bundles 链尾等直调路径不建 job 行、没有 job.status
       收尾事件，进度事件置起的「进行中」靠这一条拉回 */
    source.addEventListener('crawl.idle', () => {
      running.value = false
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
