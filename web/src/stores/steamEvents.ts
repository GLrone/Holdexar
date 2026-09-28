import { defineStore } from 'pinia'
import { ref } from 'vue'

import { steamEventsApi, type SteamEventsPayload } from '@/api/client'

/**
 * Steam 活动日历数据（日历页与仪表盘倒计时卡共享同一份）。
 *
 * 后端是只读展示链：官方文档页每日一拍同步落库，这里挂载即拉 + 每小时
 * 轮询跟随。store 常驻（pinia 单例），仪表盘卡与日历页不会各自拉一遍。
 * state 语义对齐 Epic 卡片：loading=首次拉取中 / ok=有数据（含同步源
 * 失败时保留的旧数据，stale 标记区分）/ failed=无任何数据可显。
 */

const HOUR_MS = 3_600_000

export const useSteamEventsStore = defineStore('steamEvents', () => {
  const payload = ref<SteamEventsPayload | null>(null)
  /** loading=首次拉取中 / ok=有数据（含 stale 旧数据）/ failed=无数据可显 */
  const state = ref<'loading' | 'ok' | 'failed'>('loading')

  async function load(): Promise<void> {
    try {
      const res = await steamEventsApi.list()
      payload.value = res
      state.value = 'ok'
    } catch {
      if (payload.value === null) state.value = 'failed'
    }
  }

  let started = false
  let timer: number | undefined

  /** 首个消费者挂载时启动：即拉一次 + 每小时轮询（store 生命周期内只起一次） */
  function start(): void {
    if (started) return
    started = true
    void load()
    timer = window.setInterval(() => void load(), HOUR_MS)
  }

  function stopPolling(): void {
    if (timer !== undefined) {
      window.clearInterval(timer)
      timer = undefined
    }
    started = false
  }

  return { payload, state, load, start, stopPolling }
})
