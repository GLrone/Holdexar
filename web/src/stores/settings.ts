import { defineStore } from 'pinia'
import { ref } from 'vue'

import { settingsApi, type SettingsPayload } from '@/api/client'

/** 应用设置（账户 + 新手教程标志 + 自动价格链开关 + 更新提示锚点）。区服配置已迁至 stores/regions（服务端下发 + crawl_regions 表）。 */
export const useSettingsStore = defineStore('settings', () => {
  const account = ref<SettingsPayload['account'] | null>(null)
  const loaded = ref(false)
  /** 新手教程完成标志（settingsPayload 初次拉取后填充；null = 未拉到） */
  const onboardingDone = ref<boolean | null>(null)
  /** 自动价格链开关（null = 未拉到，UI 按开处理；定时爬价 + 失败修复的总闸） */
  const autoPrice = ref<boolean | null>(null)
  /** 已主动提示过的版本号（'' = 从未提示；启动告知据此做「一次一版本」去重） */
  const updateNotified = ref('')

  async function load() {
    if (loaded.value) return
    try {
      const s = await settingsApi.get()
      account.value = s.account
      onboardingDone.value = s.onboarding_done
      autoPrice.value = s.auto_price
      updateNotified.value = s.update_notified
      loaded.value = true
    } catch {
      /* 静默 */
    }
  }

  /** 新手教程完成：置标志并落库（失败静默——下次启动会再弹，可接受） */
  async function markOnboardingDone() {
    onboardingDone.value = true
    try {
      const s = await settingsApi.update({ onboarding_done: true })
      onboardingDone.value = s.onboarding_done
    } catch {
      /* 静默 */
    }
  }

  /** 记录本次已提示的版本号（先改本地避免同会话重复弹；落库失败静默可接受） */
  async function markUpdateNotified(version: string) {
    if (!version) return
    updateNotified.value = version
    try {
      const s = await settingsApi.update({ update_notified: version })
      updateNotified.value = s.update_notified
    } catch {
      /* 静默：失败只影响下次启动会再提示一次 */
    }
  }

  /** 自动价格链启停（返回是否成功；失败时回滚本地态由调用方处理） */
  async function setAutoPrice(on: boolean) {
    const prev = autoPrice.value
    autoPrice.value = on
    try {
      const s = await settingsApi.update({ auto_price: on })
      autoPrice.value = s.auto_price
      return true
    } catch {
      autoPrice.value = prev
      return false
    }
  }

  return {
    account,
    loaded,
    onboardingDone,
    autoPrice,
    updateNotified,
    load,
    markOnboardingDone,
    markUpdateNotified,
    setAutoPrice,
  }
})
