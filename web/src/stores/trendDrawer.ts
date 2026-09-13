import { defineStore } from 'pinia'
import { ref } from 'vue'

/**
 * 价格走势抽屉全局状态。
 * 同一时刻仅一个游戏的走势抽屉打开：
 * - 点击已打开游戏的走势键 → 关闭抽屉
 * - 点击其他游戏的走势键 → 切换为该游戏的走势
 */
export const useTrendDrawerStore = defineStore('trendDrawer', () => {
  const activeAppid = ref<number | null>(null)

  function toggle(appid: number) {
    if (activeAppid.value === appid) {
      activeAppid.value = null
    } else {
      activeAppid.value = appid
    }
  }

  function close() {
    activeAppid.value = null
  }

  function isOpen(appid: number): boolean {
    return activeAppid.value === appid
  }

  return { activeAppid, toggle, close, isOpen }
})
