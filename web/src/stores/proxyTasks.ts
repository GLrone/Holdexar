import { defineStore } from 'pinia'
import { ref } from 'vue'

import { proxiesApi, type ClashTestProgress } from '@/api/client'
import { beginTask, endTask, updateTask } from '@/components/ui/island'
import { message } from '@/components/ui'
import { useI18n } from '@/locales'

/**
 * 代理域长任务的共享生命周期（store 承载，跨页存活）：
 *
 * **Clash 节点检验**：探测本体是后端会话，离开页面照常推进；前端这边用
 * 1s 轮询进度端点驱动按钮、面板与灵动岛任务位。轮询器放 store——放组件里
 * 卸载不清理会泄漏计时器，重进页面又起新轮询，完成时新旧实例各弹一次结果。
 *
 * **内核下载**：进度由后端模块级状态提供（/clash/install/progress），手动
 * 安装与保存订阅自动下载共用同一路轮询；下载期间灵动岛任务位全局可见。
 */
export const useProxyTasksStore = defineStore('proxyTasks', () => {
  const { t } = useI18n()

  /* ── Clash 节点检验 ── */
  const clashTest = ref<ClashTestProgress | null>(null)
  const TEST_KEY = 'clash-test'
  let testTimer: number | null = null

  function stopTestPoll(): void {
    if (testTimer !== null) {
      window.clearInterval(testTimer)
      testTimer = null
    }
    endTask(TEST_KEY)
  }

  /** 轮询一拍检测进度；终态收口（完成 toast / 失败原因）并撤下任务位 */
  async function pollTestOnce(): Promise<void> {
    const snap = await proxiesApi.clashTestProgress()
    if (!snap || snap.phase === 'idle') {
      clashTest.value = null
      stopTestPoll()
      return
    }
    clashTest.value = snap
    if (snap.phase === 'queued' || snap.phase === 'running') {
      const pct = snap.toProbe
        ? Math.min(100, Math.round((snap.probed / snap.toProbe) * 100))
        : null
      updateTask(TEST_KEY, t('proxies.clash.testingProgress', { done: snap.probed, total: snap.toProbe ?? 0 }), pct)
      return
    }
    stopTestPoll()
    if (snap.phase === 'done') {
      const msg = t('proxies.clash.testDone', { alive: snap.alive, total: snap.total ?? 0 })
      if (snap.deprecated) {
        message.error(t('proxies.clash.testDoneDeprecated', { msg }))
      } else {
        message.success(msg)
      }
    } else if (snap.error) {
      message.error(snap.error)
    }
  }

  /** 单例轮询：在跑时重入不叠加第二份（重进页面 / 重复启动都落在这里） */
  function startTestPoll(): void {
    if (testTimer !== null) return
    beginTask(TEST_KEY, t('proxies.clash.testingProgress', { done: 0, total: 0 }), { to: '/proxies' })
    testTimer = window.setInterval(() => void pollTestOnce().catch(() => {}), 1000)
    void pollTestOnce().catch(() => {})
  }

  /** 外部拿到的会话快照并入（切换订阅后的首检快照 / 进页恢复） */
  function adoptTest(snap: ClashTestProgress | null): void {
    if (!snap || snap.phase === 'idle') return
    clashTest.value = snap
    if (snap.phase === 'queued' || snap.phase === 'running') startTestPoll()
  }

  /** 进页对齐：有进行中的检测会话就续上轮询与任务位 */
  async function attach(): Promise<void> {
    try {
      adoptTest(await proxiesApi.clashTestProgress())
    } catch {
      /* 进度不可得按无会话处理 */
    }
  }

  /** 启动检测：后端立即返回会话快照，探测在后台推进 */
  async function startTest(): Promise<void> {
    try {
      clashTest.value = await proxiesApi.clashTestStart()
      startTestPoll()
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    }
  }

  /** 停内核即清会话：检测随内核停止而终止 */
  function clearTest(): void {
    clashTest.value = null
    stopTestPoll()
  }

  /* ── 内核下载进度 ── */
  const kernelDownloading = ref(false)
  const kernelProgress = ref(0)
  const kernelPhase = ref('')
  const kernelVia = ref('')
  const kernelSource = ref('')
  const KERNEL_KEY = 'kernel-install'
  let kernelTimer: number | null = null

  function stopKernelWatch(): void {
    if (kernelTimer !== null) {
      window.clearInterval(kernelTimer)
      kernelTimer = null
    }
    kernelDownloading.value = false
    endTask(KERNEL_KEY)
  }

  async function pollKernelOnce(): Promise<void> {
    try {
      const p = await proxiesApi.clashInstallProgress()
      kernelProgress.value = p.percent ?? 0
      kernelPhase.value = p.phase ?? ''
      kernelVia.value = p.via ? t('proxies.kernel.viaWrap', { via: p.via }) : ''
      kernelSource.value = (p.source ?? '').replace(/^https?:\/\//, '').replace(/\/$/, '')
      updateTask(KERNEL_KEY, t('proxies.kernel.dialogTitle'), p.percent ?? null)
      if (p.error) {
        stopKernelWatch()
        message.error(t('proxies.kernel.downloadFailed', { error: p.error }))
      } else if (p.ok && !p.running) {
        kernelProgress.value = 100
        stopKernelWatch()
        message.success(t('proxies.kernel.downloadDone'))
      }
    } catch {
      /* 轮询瞬断忽略，下轮再试 */
    }
  }

  /** 挂上轮询与任务位（不重置已有进度，进页恢复走这条） */
  function startKernelWatch(): void {
    if (kernelTimer !== null) return
    kernelDownloading.value = true
    beginTask(KERNEL_KEY, t('proxies.kernel.dialogTitle'), { percent: kernelProgress.value, to: '/proxies' })
    kernelTimer = window.setInterval(() => void pollKernelOnce(), 800)
    void pollKernelOnce()
  }

  /** 用户主动发起的下载监控：进度归零从头拍 */
  function beginKernelWatch(): void {
    kernelProgress.value = 0
    kernelPhase.value = ''
    kernelVia.value = ''
    kernelSource.value = ''
    startKernelWatch()
  }

  /** 进页对齐：后端有下载在跑就续上弹窗数据与任务位 */
  async function attachKernel(): Promise<void> {
    if (kernelTimer !== null) return
    try {
      const p = await proxiesApi.clashInstallProgress()
      if (p.running) startKernelWatch()
    } catch {
      /* 进度不可得按无下载处理 */
    }
  }

  /* ── 全部订阅更新（store 承载：切页不丢进度，灵动岛同步展示） ── */
  const syncAll = ref<{ done: number; total: number } | null>(null)
  const SYNC_ALL_KEY = 'sub-sync-all'

  /**
   * 顺序重拉全部 Clash 订阅：循环体在 store 里跑，离开页面照常推进，
   * 灵动岛任务位全程展示「正在更新订阅 done/total」。单条失败不中断，
   * 收口给汇总气泡。返回失败条数（0 = 全部成功）。
   */
  async function startSyncAll(
    subs: { id: number; label: string | null }[],
  ): Promise<number> {
    if (syncAll.value !== null || subs.length === 0) return 0
    syncAll.value = { done: 0, total: subs.length }
    beginTask(
      SYNC_ALL_KEY,
      t('proxies.sub.syncAllRunning', { done: 0, total: subs.length }),
      { percent: 0, to: '/proxies' },
    )
    let failed = 0
    for (let i = 0; i < subs.length; i++) {
      updateTask(
        SYNC_ALL_KEY,
        t('proxies.sub.syncAllRunning', { done: i, total: subs.length }),
        Math.round((i / subs.length) * 100),
      )
      try {
        await proxiesApi.syncSubscription(subs[i].id)
      } catch (e) {
        failed += 1
        message.error(
          t('proxies.sub.syncAllOneFailed', {
            name: subs[i].label ?? `#${subs[i].id}`,
            error: e instanceof Error ? e.message : String(e),
          }),
        )
      }
      syncAll.value = { done: i + 1, total: subs.length }
    }
    endTask(SYNC_ALL_KEY)
    if (failed === 0) {
      message.success(t('proxies.sub.syncAllDone', { total: subs.length }))
    } else {
      message.error(t('proxies.sub.syncAllPartial', { failed }))
    }
    syncAll.value = null
    return failed
  }

  return {
    clashTest,
    kernelDownloading,
    kernelProgress,
    kernelPhase,
    kernelVia,
    kernelSource,
    syncAll,
    adoptTest,
    attach,
    startTest,
    clearTest,
    beginKernelWatch,
    startKernelWatch,
    startSyncAll,
    stopKernelWatch,
    attachKernel,
  }
})
