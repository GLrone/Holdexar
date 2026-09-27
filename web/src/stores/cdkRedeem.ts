import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { redeemApi, type RedeemQuota } from '@/api/client'
import { beginTask, endTask, updateTask } from '@/components/ui/island'
import { message } from '@/components/ui'
import { useI18n, type MessageKey } from '@/locales'

/**
 * CDK 批量激活生命周期（store 承载，跨页存活）。
 *
 * 激活是**前端驱动的批次循环**（每批 9 码并发提交、批间 20s 避让 Steam
 * 30 分钟 10 次限制），全程可达数分钟——放页面组件里，切页即丢进度且
 * 组件重建后 `running` 归零，会叠起第二轮并发循环打爆限流窗口。store 是
 * pinia 单例：循环、队列行、进度、配额计数都活在全局，切走再回来原样
 * 继续，灵动岛任务位同步展示进度与完成汇总。
 */

export type KeyStatus = 'wait' | 'doing' | 'ok' | 'own' | 'fail'

export interface KeyRow {
  code: string
  status: KeyStatus
}

export interface ResultRow {
  /** 对应的激活码原文（结果行与左栏队列按序号一一对应的锚点） */
  code: string
  status: KeyStatus
  detail: string
  subId: string
  subName: string
  /** Steam 返回原文（点开「原文」核对） */
  raw: string
}

export const CDK_BATCH_SIZE = 9
export const CDK_BATCH_WAIT = 20000
export const CDK_ACT_LIMIT = 10
export const CDK_ACT_WINDOW = 30 * 60 * 1000

const TASK_KEY = 'cdk-redeem'

export const useCdkRedeemStore = defineStore('cdkRedeem', () => {
  const { t } = useI18n()

  /* 输入区文本也入 store：切页往返不丢已粘贴的激活码，队列行不重建 */
  const keyArea = ref('')
  const keyQueue = ref<KeyRow[]>([])
  const resultRows = ref<ResultRow[]>([])
  /** 批次进度：「批次 n / N」。只存数字，句子在模板里 t() 现取（语言不冻） */
  const batchNo = ref(1)
  const batchTotal = ref(1)
  /** 进度文案同理：只存词条 key，模板 t(progressKey) 现取 */
  const progressKey = ref<MessageKey>('toolbox.cdk.progress.ready')
  const progressCount = ref('0 / 0')
  const progressPct = ref(0)
  const ksOk = ref(0)
  const ksFail = ref(0)
  const ksOwn = ref(0)
  const running = ref(false)

  const quotaReady = ref(true)
  /** 未绑 Cookie 提示的词条 key（'' = 无提示） */
  const quotaMsgKey = ref<'' | MessageKey>('')
  /** 当前绑定账号（激活计数按账号独立，切号即重计） */
  const quotaSteamId = ref('')
  /** 账号维度已用次数：后端为准 + 本会话内增量（多批激活即时累计） */
  const usedBase = ref(0)
  const usedSession = ref<number[]>([])

  /** 30 分钟窗口内本账号已用激活次数（后端基数 + 会话增量） */
  const usedCount = computed(() => {
    const now = Date.now()
    const sess = usedSession.value.filter((ts) => now - ts < CDK_ACT_WINDOW).length
    return usedBase.value + sess
  })
  const nearLimit = computed(() => usedCount.value >= CDK_ACT_LIMIT - 2)
  const quotaMsg = computed(() => (quotaMsgKey.value ? t(quotaMsgKey.value) : ''))

  /** 智能识别激活码：5-5-5（及多段）格式，中间连字符不可省略 */
  function parseKeys(): string[] {
    const text = keyArea.value.trim().toUpperCase()
    const reg = /([0-9A-Z]{5}-){2,4}[0-9A-Z]{5}/g
    const keys: string[] = []
    let m: RegExpExecArray | null
    while ((m = reg.exec(text)) !== null) {
      keys.push(m[0])
    }
    return keys
  }

  /** 输入即渲染左栏队列 + 右栏等待行（运行中不重建，防清掉在跑的批次） */
  function renderKeyQueue(): void {
    if (running.value) return
    const keys = parseKeys()
    keyQueue.value = keys.map((k) => ({ code: k, status: 'wait' as KeyStatus }))
    resultRows.value = keys.map((k) => ({
      code: k,
      status: 'wait' as KeyStatus,
      detail: '',
      subId: '',
      subName: '',
      raw: '',
    }))
    progressCount.value = `0 / ${keys.length}`
    batchNo.value = 1
    batchTotal.value = Math.max(1, Math.ceil(keys.length / CDK_BATCH_SIZE))
  }

  async function loadQuota(): Promise<void> {
    try {
      const q: RedeemQuota = await redeemApi.quota()
      quotaReady.value = q.hasCookie && q.hasSessionId
      quotaMsgKey.value = quotaReady.value ? '' : 'toolbox.cdk.quotaMissing'
      // 换绑账号 → 会话增量作废、以后端计数为新基数
      if (q.steamId !== quotaSteamId.value) {
        quotaSteamId.value = q.steamId
        usedSession.value = []
      }
      usedBase.value = q.used
    } catch {
      quotaMsgKey.value = ''
    }
  }

  /** 切号 / 会话失效：会话增量作废并重取后端计数 */
  async function resetSession(): Promise<void> {
    usedSession.value = []
    await loadQuota()
  }

  /** 批量激活主循环：请求、进度（页内 + 灵动岛任务位）、结果消息都在这里 */
  async function startRedeem(): Promise<void> {
    if (running.value) return
    const keys = parseKeys()
    if (keys.length === 0) return
    if (!quotaReady.value) {
      progressKey.value = quotaMsgKey.value || 'toolbox.cdk.progress.noCookie'
      message.warning(t('toolbox.cdk.bindFirst'))
      return
    }
    const available = CDK_ACT_LIMIT - usedCount.value
    if (available <= 0) {
      progressKey.value = 'toolbox.cdk.progress.limitReached'
      return
    }
    const toActivate = Math.min(keys.length, available)
    running.value = true
    progressPct.value = 0
    progressKey.value = 'toolbox.cdk.progress.running'
    beginTask(TASK_KEY, t('toolbox.cdk.progress.running'), { percent: 0, to: '/toolbox' })
    let done = 0
    const totalBatches = Math.ceil(toActivate / CDK_BATCH_SIZE)

    for (let b = 0; b < totalBatches; b++) {
      const start = b * CDK_BATCH_SIZE
      const batch = keys.slice(start, start + CDK_BATCH_SIZE)
      batchNo.value = b + 1
      batchTotal.value = totalBatches

      // 每个码一个独立请求并发发出——谁先回来谁先上屏，不等整批
      const tasks = batch.map(async (code, i) => {
        const qi = start + i
        if (keyQueue.value[qi]) keyQueue.value[qi]!.status = 'doing'
        if (resultRows.value[qi]) resultRows.value[qi]!.status = 'doing'
        let r: { status: string; detail: string; subId: string; subName: string; raw?: string }
        try {
          const res = await redeemApi.activateKeys([code])
          r = res.results[0] ?? {
            status: 'fail',
            // 即时回执，不跨语言切换长驻：事件回调里取词条
            detail: t('toolbox.cdk.failNoResult'),
            subId: '',
            subName: '',
            raw: '',
          }
        } catch (e) {
          r = {
            status: 'fail',
            detail: e instanceof Error ? e.message : String(e),
            subId: '',
            subName: '',
            raw: '',
          }
        }
        const st = (r.status === 'ok' || r.status === 'own' ? r.status : 'fail') as KeyStatus
        usedSession.value.push(Date.now())
        if (keyQueue.value[qi]) keyQueue.value[qi]!.status = st
        if (resultRows.value[qi]) {
          resultRows.value[qi] = {
            code,
            status: st,
            detail: r.detail || '——',
            subId: r.subId || '',
            subName: r.subName || '',
            raw: r.raw || '',
          }
        }
        if (st === 'ok') ksOk.value++
        else if (st === 'own') ksOwn.value++
        else ksFail.value++
        done++
        progressPct.value = Math.round((done / toActivate) * 100)
        progressCount.value = `${done} / ${toActivate}`
        updateTask(TASK_KEY, t('toolbox.cdk.progress.running'), progressPct.value)
      })
      await Promise.all(tasks)

      if (b < totalBatches - 1) {
        progressKey.value = 'toolbox.cdk.progress.batchWait'
        updateTask(TASK_KEY, t('toolbox.cdk.progress.batchWait'), progressPct.value)
        await new Promise((r) => setTimeout(r, CDK_BATCH_WAIT))
      }
    }
    progressKey.value = 'toolbox.cdk.progress.done'
    running.value = false
    endTask(TASK_KEY)

    /* 完成汇总气泡：全部成功 / 部分失败 / 全失败 三档，各成一条参数化词条 */
    const skipped = keys.length - toActivate
    const params = { ok: ksOk.value, own: ksOwn.value, fail: ksFail.value, skipped }
    if (ksFail.value === 0) {
      message.success(t(skipped > 0 ? 'toolbox.cdk.done.okSkipped' : 'toolbox.cdk.done.ok', params))
    } else if (ksOk.value + ksOwn.value > 0) {
      message.warning(
        t(skipped > 0 ? 'toolbox.cdk.done.partialSkipped' : 'toolbox.cdk.done.partial', params),
      )
    } else {
      message.error(
        t(skipped > 0 ? 'toolbox.cdk.done.allFailedSkipped' : 'toolbox.cdk.done.allFailed', params),
      )
    }
    void loadQuota()
  }

  /** 清空重置（运行中拒绝：会清掉在跑批次的状态行） */
  function resetRedeem(): void {
    if (running.value) return
    usedSession.value = []
    keyQueue.value = []
    resultRows.value = []
    progressPct.value = 0
    progressKey.value = 'toolbox.cdk.progress.ready'
    ksOk.value = 0
    ksFail.value = 0
    ksOwn.value = 0
    progressCount.value = '0 / 0'
    batchNo.value = 1
    batchTotal.value = 1
    void loadQuota()
    renderKeyQueue()
  }

  return {
    keyArea,
    keyQueue,
    resultRows,
    batchNo,
    batchTotal,
    progressKey,
    progressCount,
    progressPct,
    ksOk,
    ksFail,
    ksOwn,
    running,
    quotaReady,
    quotaMsgKey,
    quotaMsg,
    usedCount,
    nearLimit,
    parseKeys,
    renderKeyQueue,
    loadQuota,
    resetSession,
    startRedeem,
    resetRedeem,
  }
})
