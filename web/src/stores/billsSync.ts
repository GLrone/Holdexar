import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  billsApi,
  type BillSyncResult,
  type BillSyncSnapshot,
} from '@/api/client'
import { beginTask, endTask, updateTask } from '@/components/ui/island'
import { message } from '@/components/ui'
import { useI18n } from '@/locales'

/**
 * 账单同步生命周期（store 承载，跨页存活）。
 *
 * 同步本体是后端常驻任务（Cookie 在线全量翻页，可达数分钟），前端这边
 * 负责：发起请求 → 1s 轮询快照把翻页阶段喂给灵动岛任务位 → 结果消息收场。
 * 放 store 而不是页面组件：组件一切页就卸载，进度展示与结果反馈会一起丢；
 * store 是 pinia 单例，切走再回来进度照跑，与 achievements store 同一套语义。
 *
 * 重入防护：`syncing`（本次发起未收场）与快照 `running`（含定时任务的
 * 自动同步）任一为真都不再发起——后端对 running 周期内的手动触发返回
 * busy，前端按「续看进度」处理，不弹错误。
 */
export const useBillsSyncStore = defineStore('billsSync', () => {
  const { t } = useI18n()

  const syncing = ref(false)
  const snapshot = ref<BillSyncSnapshot | null>(null)

  const TASK_KEY = 'bills-sync'
  let pollTimer: number | null = null

  /** 翻页阶段 → 用户语言（后端把阶段写进快照，Steam 游标翻页无总页数） */
  function stageText(snap: BillSyncSnapshot): string {
    if (snap.stage === 'history') {
      return t('bills.sync.stageHistory', { pages: snap.pages ?? 0, rows: snap.rows ?? 0 })
    }
    if (snap.stage === 'licenses') return t('bills.sync.stageLicenses')
    if (snap.stage === 'import') return t('bills.sync.stageImport')
    return t('bills.sync.stageIdentity')
  }

  function stopDisplayPolling(): void {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
    endTask(TASK_KEY)
  }

  async function pollOnce(): Promise<void> {
    try {
      const snap = await billsApi.syncStatus()
      snapshot.value = snap
      if (snap.running) {
        updateTask(TASK_KEY, stageText(snap))
        return
      }
      // 自动同步在页面之外收场时没人 await：轮询方负责撤下进度位
      stopDisplayPolling()
    } catch {
      /* 单次轮询失败不打断同步，下一秒再取 */
    }
  }

  function startDisplayPolling(): void {
    if (pollTimer !== null) return
    beginTask(TASK_KEY, snapshot.value?.running ? stageText(snapshot.value) : t('bills.sync.stageIdentity'), {
      to: '/bills',
    })
    pollTimer = window.setInterval(() => void pollOnce(), 1000)
  }

  /** 进页对齐：后端有在跑的同步（定时任务 / 其他入口）就续上进度展示 */
  async function attach(): Promise<void> {
    try {
      const snap = await billsApi.syncStatus()
      snapshot.value = snap
      if (snap.running) startDisplayPolling()
    } catch {
      snapshot.value = null
    }
  }

  async function loadSnapshot(): Promise<void> {
    try {
      snapshot.value = await billsApi.syncStatus()
    } catch {
      snapshot.value = null
    }
  }

  /**
   * 手动同步全流程：请求、进度展示、结果消息都在这里，切页不中断。
   * 返回同步结果供发起页面刷新数据；返回 null = 未发起（重入）或按
   * 「续看进度」收场（busy），不弹错误。
   */
  async function syncNow(): Promise<BillSyncResult | null> {
    if (syncing.value || snapshot.value?.running) return null
    syncing.value = true
    startDisplayPolling()
    try {
      const res = await billsApi.syncBills()
      if (res.status === 'busy') return null
      message.success(
        t('bills.sync.success', {
          nickname: res.nickname,
          bills: res.gameTxs,
          cdk: res.cdkGames,
          rows: res.historyRows,
        }),
      )
      return res
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
      return null
    } finally {
      syncing.value = false
      // 快照仍在跑（busy 续看）：进度位交给轮询自收口；否则就地撤下
      if (snapshot.value?.running) void pollOnce()
      else stopDisplayPolling()
      void loadSnapshot()
    }
  }

  return { syncing, snapshot, attach, syncNow, loadSnapshot }
})
