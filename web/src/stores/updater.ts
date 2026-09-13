import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { systemApi, type UpdateCheckResult } from '@/api/client'

/**
 * 应用更新状态（检查 + 启动告知依据）。
 *
 * 与「我」页更新卡片的职责分工：卡片负责展示细节与执行下载/重启，
 * 本 store 只负责**应用启动时查一次**，供侧栏红点与主动提示使用。
 * 检查结果缓存在这里，卡片主动检查时也会回写同一份（见 views/settings）。
 *
 * 设计要点：
 * - `hasUpdate` 跟随实际可用状态，用户没更新就一直亮红点
 * - 「已提示过」的判定不在这里，而在 settings store 的 ui.update_notified
 *   （跨启动持久化，本 store 是内存态，重启即空）
 */
export const useUpdaterStore = defineStore('updater', () => {
  const info = ref<UpdateCheckResult | null>(null)
  const checking = ref(false)
  /** 本次会话是否已查过（避免重复自动检查；手动检查传 force） */
  const checked = ref(false)

  /** 有可用新版 = 侧栏红点依据 */
  const hasUpdate = computed(() => info.value?.available === true)

  /**
   * 检查更新。网络不可达时后端降级返回 available=false（不抛），
   * 所以这里只需兜住 request 层异常，失败静默——检查更新不该打断启动。
   */
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

  return { info, checking, checked, hasUpdate, check }
})
