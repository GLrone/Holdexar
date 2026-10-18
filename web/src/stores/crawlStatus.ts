import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { crawlApi } from '@/api/client'
import { message } from '@/components/ui'
import { activityManager } from '@/components/ui/activity'
import { beginTask, endTask, updateTask } from '@/components/ui/island'
import { CYCLE_ACTIVE } from '@/lib/headerStatus'
import { useI18n } from '@/locales'

import { nextPriceCycle, type PriceCycleMark } from '@/lib/priceRefresh'

/** 体检/重建进行中的前端状态：kind + 两层已探测计数（累计口径）。
    totalL1/totalL2 是后端下发的各层目标量（旧后端不下发，缺省 = 无百分比）。 */
interface MaintenanceState {
  kind: string
  probedL1: number
  probedL2: number
  totalL1?: number
  totalL2?: number
}

/** 灵动岛长任务注册键：体检跨页存活，进行中不自动消失 */
const MAINTENANCE_TASK_KEY = 'pool-maintenance'

/** 爬取状态（SSE 驱动）：顶栏胶囊 + 爬取任务页共用。 */
export const useCrawlStatusStore = defineStore('crawlStatus', () => {
  const running = ref(false)
  const done = ref(0)
  const ok = ref(0)
  const fail = ref(0)
  const qsize = ref(0)
  const speed = ref(0)
  /* 平滑速度（计数/秒）：进度事件的 speed 是近 10s 滑窗的瞬时值，直接拿来
     算预计结束时间会随窗口跳动；按 0.3 权重做指数平滑。速度为 0（刚起步、
     段间空档、停摆）既不参与平滑也不给结论——宁可不说，不报假时间 */
  const etaSpeed = ref(0)
  /* 任务目标量：后端在任务启动时定死（初始任务数），随进度事件下发。
     不能用 done+qsize 拼总数——队列排干/逐层入队/重推都会让它波动。 */
  const total = ref(0)
  const activeJobId = ref<number | null>(null)
  const lastEventAt = ref<string>('')

  /* 体检/重建进行中（SSE maintenance.* 实时事件 + REST 账本对账双通道喂入）：
     事实源是后端体检账本（health_runs + 逐块落库的观测行），这里只投影。
     任务页系统状态行与灵动岛长任务共用这一份，任务页 / 顶栏之外任何页面
     都能从岛上看到体检在跑、跑到哪。 */
  const maintenance = ref<MaintenanceState | null>(null)

  /* 轮收尾段（标记刷新/事件检测/统计，分钟级且无抓取在跑）：SSE
     price_cycle.finalizing 置位、终态事件清除；价格状态胶囊据此给「汇总中」
     而不是停在「正在更新」。syncRoundScope 以轮账本为准对账页面刷新。 */
  const roundFinalizing = ref(false)

  /* 最近一次收敛的价格刷新周期（SSE price_cycle.completed）：库视图据此失效
     列表缓存并原地重拉。按 cycleId 去重——同一轮重复到达不触发第二次请求。 */
  const priceCycle = ref<PriceCycleMark | null>(null)

  /* 价格数据版本号：price_cycle.completed 与任务终态（含无 Cycle 的手动/
     补抓任务——它们写价但不产生周期收敛事件）都会推进。视图只盯这一个
     信号做原地重拉，不再各自判断「这轮/这个任务有没有写价」。 */
  const dataEpoch = ref(0)

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

  /** 预计剩余秒数：剩余量取轮内累计口径，速度取当前段（段间速度不同，
      轮内换段后随新段速度重新收敛）。null = 算不出来（无速度 / 已跑完 /
      超出 24h 不报）。 */
  const etaSeconds = computed(() => {
    if (!running.value || etaSpeed.value <= 0) return null
    const p = roundProgress.value ?? { done: done.value, total: total.value }
    const remain = p.total - p.done
    if (remain <= 0) return null
    const secs = Math.ceil(remain / etaSpeed.value)
    return secs <= 86400 ? secs : null
  })

  /* 以最近一轮的记账状态核对轮次是否在跑，并把该轮已收尾任务段的完成量补进
     累计（页面半途打开时，前几段的量从这里补齐）。查询失败保持现状。 */
  async function syncRoundScope() {
    try {
      const [cycle] = await crawlApi.cycles(1)
      if (!cycle || !CYCLE_ACTIVE.has(cycle.status)) {
        roundTracking.value = false
        roundFinalizing.value = false
        return
      }
      roundFinalizing.value = cycle.status === 'finalizing'
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

  /* ── 体检/维护进行中：账本投影的唯一落点 ──────────────────────────
     SSE 事件（started/progress/ended）与 REST 对账（/crawl/active 的
     maintenanceInfo）都汇到这里，同一事实一个落点，页面轮询与事件推送
     不各养一份状态。灵动岛长任务随状态注册/续接/撤下。 */

  /** 岛任务的文案与定量进度。三态：L2 进行中（有 L2 计数，或 L1 已探完而
      下一层首个进度事件未到）按 L2 口径出 n/total 与百分比；总数未下发的
      旧后端保持累计计数兜底（词条单参数口径）；有总数的 L1 阶段是出口
      身份核对，不给会误导的「节点」计数。 */
  function maintenanceTaskView(state: MaintenanceState): { text: string; percent: number | null } {
    const { t } = useI18n()
    if (state.kind === 'rebuild') return { text: t('island.maintenance.rebuild'), percent: null }
    const totalL1 = state.totalL1 ?? 0
    const totalL2 = state.totalL2 ?? 0
    const l2Underway = state.probedL2 > 0 || (totalL1 > 0 && state.probedL1 >= totalL1)
    if (l2Underway && totalL2 > 0) {
      return {
        text: t('island.maintenance.healthProbed', { n: state.probedL2, total: totalL2 }),
        percent: Math.min(100, Math.round((state.probedL2 / totalL2) * 100)),
      }
    }
    if (totalL1 === 0 && totalL2 === 0) {
      const n = state.probedL1 + state.probedL2
      return {
        text:
          n > 0
            ? t('island.maintenance.healthProbedCount', { n })
            : t('island.maintenance.health'),
        percent: null,
      }
    }
    return { text: t('island.maintenance.healthExit'), percent: null }
  }

  /** 维护状态统一落点（null = 收场）：岛任务随状态注册/续接/撤下 */
  function setMaintenance(state: MaintenanceState | null): void {
    const fresh = state !== null && maintenance.value === null
    const cleared = state === null && maintenance.value !== null
    maintenance.value = state
    if (fresh) {
      const view = maintenanceTaskView(state!)
      beginTask(MAINTENANCE_TASK_KEY, view.text, {
        percent: view.percent,
        to: '/crawl',
      })
    } else if (state) {
      const view = maintenanceTaskView(state)
      updateTask(MAINTENANCE_TASK_KEY, view.text, view.percent)
    } else if (cleared) {
      endTask(MAINTENANCE_TASK_KEY)
    }
  }

  /** REST 投影适配：/crawl/active.maintenanceInfo → 统一落点 */
  function applyMaintenanceInfo(
    info: {
      kind?: string
      probed?: { l1?: number; l2?: number }
      totals?: { l1?: number; l2?: number }
    } | null,
  ): void {
    setMaintenance(
      info
        ? {
            kind: info.kind ?? 'health',
            probedL1: info.probed?.l1 ?? 0,
            probedL2: info.probed?.l2 ?? 0,
            totalL1: typeof info.totals?.l1 === 'number' ? info.totals.l1 : undefined,
            totalL2: typeof info.totals?.l2 === 'number' ? info.totals.l2 : undefined,
          }
        : null,
    )
  }

  /** 维护收场：撤下进行中气泡，结果入消息队列（有节点结论才报数字） */
  function announceMaintenanceEnded(
    kind: string,
    data: { durationMs?: number; ok?: number; failed?: number },
  ): void {
    const { t } = useI18n()
    const text =
      kind === 'rebuild'
        ? t('island.maintenance.rebuildDone')
        : typeof data.ok === 'number'
          ? t('island.maintenance.healthDone', {
              ok: data.ok,
              fail: data.failed ?? 0,
              min: data.durationMs ? Math.max(1, Math.round(data.durationMs / 60000)) : 0,
            })
          : t('island.maintenance.healthDoneSimple')
    message.success(text, { duration: 8000, to: '/crawl' })
  }

  let source: EventSource | null = null
  let started = false

  const CRAWL_ACTIVITY_ID = 'crawler:job'

  function syncCrawlActivity(): void {
    if (running.value && total.value > 0) {
      const pDone = roundTracking.value ? roundDone.value + done.value : done.value
      const pTotal = roundTracking.value ? Math.max(roundTotal.value, startedTotal.value, total.value) : total.value
      const pct = pTotal > 0 ? Math.min(100, Math.round((pDone / pTotal) * 100)) : null
      const { t } = useI18n()
      activityManager.register({
        id: CRAWL_ACTIVITY_ID,
        source: 'crawler',
        kind: 'task',
        status: 'running',
        title: t('island.task.running', { done: pDone, total: pTotal }),
        progress: pct,
        to: '/crawl',
        priority: 30,
      })
    } else {
      activityManager.remove(CRAWL_ACTIVITY_ID)
    }
  }

  /** SSE 断线重连后事件可能有缺口（收尾事件丢失会让「进行中」标志卡住）：
      连接建立即以执行占用的 REST 账本对账；轮次快照的重读由 running 的
      watch 链与消费方的定时兜底完成。 */
  async function resyncRunning() {
    try {
      const a = await crawlApi.active()
      running.value = a.busy === true || a.activeJobId !== null
      syncCrawlActivity()
      /* 断线窗口里的体检开始/结束事件可能丢失：以维护账本投影对账 */
      applyMaintenanceInfo(a.maintenanceInfo ?? null)
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
      if (speed.value > 0) {
        etaSpeed.value = etaSpeed.value > 0 ? etaSpeed.value * 0.7 + speed.value * 0.3 : speed.value
      }
      total.value = data.total ?? done.value + qsize.value
      lastEventAt.value = new Date().toLocaleTimeString()
      syncCrawlActivity()
    })

    source.addEventListener('job.started', (e) => {
      const data = JSON.parse((e as MessageEvent).data)
      activeJobId.value = data.job_id ?? null
      running.value = true
      done.value = 0
      ok.value = 0
      fail.value = 0
      total.value = 0
      etaSpeed.value = 0 // 新段速度未知：先不报预计结束时间
      void syncRoundScope()
      syncCrawlActivity()
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
        syncCrawlActivity()
        /* 任务终态=一轮写价落地：无 Cycle 的任务只有这一条收敛信号 */
        dataEpoch.value++
      }
    })

    /* 生产爬取入口空闲：bundles 链尾等直调路径不建 job 行、没有 job.status
       收尾事件，进度事件置起的「进行中」靠这一条拉回 */
    source.addEventListener('crawl.idle', () => {
      running.value = false
      syncCrawlActivity()
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
        roundFinalizing.value = false
        dataEpoch.value++
      }
    })

    /* 轮进入收尾段：抓取已全部结束，标记刷新/事件检测/统计在跑——
       分钟级窗口，价格状态胶囊据此切「汇总中」而不是停在更新态 */
    source.addEventListener('price_cycle.finalizing', () => {
      roundFinalizing.value = true
    })

    /* 体检/重建三拍：开始注册岛任务、逐块进度续接、收场撤下并报结果。
       进度事件走可丢通道（丢一条只是少刷一次），开始/结束走关键通道。 */
    source.addEventListener('maintenance.started', (e) => {
      const data = JSON.parse((e as MessageEvent).data)
      setMaintenance({
        kind: data.kind ?? 'health',
        probedL1: 0,
        probedL2: 0,
        /* totals 可能缺省（旧后端）：缺哪层就保持无总数口径 */
        totalL1: typeof data.totals?.l1 === 'number' ? data.totals.l1 : undefined,
        totalL2: typeof data.totals?.l2 === 'number' ? data.totals.l2 : undefined,
      })
    })

    source.addEventListener('maintenance.progress', (e) => {
      const data = JSON.parse((e as MessageEvent).data)
      const prev = maintenance.value ?? {
        kind: data.kind ?? 'health',
        probedL1: 0,
        probedL2: 0,
      }
      setMaintenance({
        ...prev,
        /* 事件带的是该层的累计探测数；只刷新在位的那一层，总数随层更新 */
        ...(data.level === 'L1'
          ? {
              probedL1: data.probed ?? prev.probedL1,
              ...(typeof data.total === 'number' ? { totalL1: data.total } : {}),
            }
          : {
              probedL2: data.probed ?? prev.probedL2,
              ...(typeof data.total === 'number' ? { totalL2: data.total } : {}),
            }),
      })
    })

    source.addEventListener('maintenance.ended', (e) => {
      const data = JSON.parse((e as MessageEvent).data)
      const kind = maintenance.value?.kind ?? data.kind ?? 'health'
      setMaintenance(null)
      announceMaintenanceEnded(kind, data)
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
    dataEpoch,
    roundProgress,
    etaSeconds,
    maintenance,
    roundFinalizing,
    applyMaintenanceInfo,
    start,
  }
})
