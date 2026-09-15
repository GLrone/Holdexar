import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { systemApi, type UpdateCheckResult, type UpdateProgress } from '@/api/client'

/**
 * 应用更新状态（检查 + 下载进度 + 待重启暂存）。
 *
 * **下载态为什么放 store 而不是设置页组件里**：下载是后端的 fire-and-forget 任务，
 * 前端组件一离开页面就卸载——放组件里必然「切走再回来进度条没了、再点下载还被
 * 后端 409（已有下载在进行）顶回来」。store 是 pinia 单例、跨页存活：进设置页时
 * 只要后端还在 `running` 就接着轮询，进度自然续上，也不会重复发起下载。
 *
 * 与「我」页更新卡片的职责分工：卡片负责展示细节与执行下载/重启，
 * 本 store 只负责**应用启动时查一次**，供侧栏红点与主动提示使用。
 * 检查结果缓存在这里，卡片主动检查时也会回写同一份（见 views/settings）。
 *
 * 设计要点：
 * - `hasUpdate` 跟随实际可用状态，用户没更新就一直亮红点
 * - 「已提示过」的判定不在这里，而在 settings store 的 ui.update_notified
 *   （跨启动持久化，本 store 是内存态，重启即空）
 * - `pendingTag` 非空 = 新版本已下载校验完成、等待重启换装：全局弹窗与
 *   设置页卡片都以此为准（后端 /system/update-pending 是唯一事实来源）
 */
export const useUpdaterStore = defineStore('updater', () => {
  const info = ref<UpdateCheckResult | null>(null)
  const checking = ref(false)
  /** 本次会话是否已查过（避免重复自动检查；手动检查传 force） */
  const checked = ref(false)

  /** 有可用新版 = 侧栏红点依据 */
  const hasUpdate = computed(() => info.value?.available === true)

  /** 下载进度（后端单例，最近一次状态；切换页面不丢） */
  const progress = ref<UpdateProgress | null>(null)
  /** 正在下载（后端 running 的前端镜像；发起后立即置位，避免按钮空窗） */
  const downloading = ref(false)
  /** 暂存就绪的版本 tag（非空 = 下载完成待重启） */
  const pendingTag = ref('')
  /** 全局「已下载，需重启」弹窗是否已被用户关掉（本次会话内不再打扰） */
  const pendingDialogDismissed = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null

  function stopPolling(): void {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  /** 读一次进度；跑完即停轮询并把「待重启」置位 */
  async function poll(): Promise<void> {
    try {
      const p = await systemApi.updateProgress()
      progress.value = p
      if (p.running) {
        downloading.value = true
        return
      }
      downloading.value = false
      stopPolling()
      if (p.ok) {
        await refreshPending()
      }
    } catch {
      /* 轮询失败（切页/网络）：不停轮询也没意义，交给下次 sync 重置 */
      downloading.value = false
      stopPolling()
    }
  }

  function startPolling(): void {
    stopPolling()
    timer = setInterval(() => void poll(), 800)
  }

  /** 与后端对齐（应用启动、进设置页时调用）：续上别人发起的下载 */
  async function sync(): Promise<void> {
    await refreshPending()
    try {
      const p = await systemApi.updateProgress()
      progress.value = p
      if (p.running) {
        downloading.value = true
        startPolling()
      } else {
        downloading.value = false
      }
    } catch {
      /* 拉不到进度不影响页面其余部分 */
    }
  }

  /** 暂存状态刷新（换装完成或用户取消后后端即为准） */
  async function refreshPending(): Promise<void> {
    try {
      const p = await systemApi.updatePending()
      pendingTag.value = p.pending ? (p.tag || '') : ''
    } catch {
      /* 拉不到保持现值 */
    }
  }

  /** 校验值：优先用清单下发的 sha256；旧发布（无清单）从 release body 的 SHA256 行兜底 */
  function extractSha(notes: string | undefined): string | null {
    if (!notes) return null
    const m = notes.match(/^SHA256:\s*([0-9a-fA-F]{64})\s*$/m)
    return m ? m[1] : null
  }

  /**
   * 发起下载。后端若回 409（已有任务在跑）**不算失败**——那正是「切页回来后
   * 又点了一次」的常态，直接切回跟着进度走。**判据不靠错误文案**（文案会随
   * 语言变）：出错后与后端对齐一次，后端在跑就当续看进度。
   */
  async function download(): Promise<{ ok: boolean; error?: string }> {
    const latest = info.value
    if (!latest?.tag) return { ok: false, error: 'no_tag' }
    downloading.value = true
    try {
      await systemApi.updateDownload(
        latest.tag,
        latest.sha256 ?? extractSha(latest.notes),
        latest.asset,
        latest.sizeBytes,
      )
      startPolling()
      void poll()
      return { ok: true }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      await sync()
      if (downloading.value || progress.value?.running) {
        startPolling()
        return { ok: true }
      }
      downloading.value = false
      return { ok: false, error: msg }
    }
  }

  /** 放弃本次更新：清暂存（后端删目录），前端状态一并复位 */
  async function cancel(): Promise<void> {
    await systemApi.updateCancel()
    stopPolling()
    progress.value = null
    downloading.value = false
    pendingTag.value = ''
  }

  /** 关掉全局弹窗但保留暂存（「稍后重启」，更新包不被丢弃） */
  function dismissPendingDialog(): void {
    pendingDialogDismissed.value = true
  }

  /**
   * 重启换装：桌面壳的 pywebview 桥（restart_app 会拉起新进程并让本进程退出，
   * 新进程启动时完成换装）。浏览器态没有这个桥，返回 unsupported 交给调用方提示。
   */
  async function restartForUpdate(): Promise<{
    ok: boolean
    unsupported?: boolean
    error?: string
  }> {
    const bridge = (
      window as unknown as {
        pywebview?: { api?: { restart_app?: () => Promise<{ ok: boolean; error?: string }> } }
      }
    ).pywebview
    try {
      if (!bridge?.api?.restart_app) return { ok: false, unsupported: true }
      const res = await bridge.api.restart_app()
      return res.ok ? { ok: true } : { ok: false, error: res.error }
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) }
    }
  }

  /** 检查更新。网络不可达时后端降级返回 available=false（不抛），
   * 所以这里只需兜住 request 层异常，失败静默——检查更新不该打断启动。 */
  async function check(force = false): Promise<UpdateCheckResult | null> {
    if (checked.value && !force) return info.value
    if (checking.value) return info.value
    checking.value = true
    try {
      info.value = await systemApi.updateCheck()
      checked.value = true
      return info.value
    } catch {
      return null
    } finally {
      checking.value = false
    }
  }

  return {
    info,
    checking,
    checked,
    hasUpdate,
    check,
    progress,
    downloading,
    pendingTag,
    pendingDialogDismissed,
    sync,
    refreshPending,
    download,
    cancel,
    dismissPendingDialog,
    restartForUpdate,
  }
})
