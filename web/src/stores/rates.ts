import { defineStore } from 'pinia'
import { ref } from 'vue'

import { APP_SLUG } from '@/appInfo'

const STORAGE_KEY = `${APP_SLUG}.rates.tracked`

/** 默认追踪集（原「主要汇率」固定列表；TRY 已改为预留币种，从默认追踪移除） */
export const DEFAULT_TRACKED = ['USD', 'RUB', 'UAH', 'KZT', 'JPY', 'HKD', 'TWD', 'INR', 'BRL']

function load(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed: unknown = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        return parsed.map((c) => String(c).toUpperCase())
      }
    }
  } catch {
    /* 静默：损坏时回退默认 */
  }
  return [...DEFAULT_TRACKED]
}

/**
 * 追踪币种偏好（汇率页自选展示集，localStorage 持久化）。
 * 仅影响前端展示——服务端始终抓取落库全量白名单币种。
 */
export const useRatesStore = defineStore('rates', () => {
  const tracked = ref<string[]>(load())

  function setTracked(codes: string[]) {
    tracked.value = codes
    localStorage.setItem(STORAGE_KEY, JSON.stringify(codes))
  }

  function isTracked(code: string): boolean {
    return tracked.value.includes(code)
  }

  return { tracked, setTracked, isTracked }
})
