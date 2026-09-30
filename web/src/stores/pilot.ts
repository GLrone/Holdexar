import { defineStore } from 'pinia'
import { ref } from 'vue'

import type { PilotGameContext } from '@/api/client'

/**
 * 领航员全局入口状态：顶栏 / 找游戏 / 游戏详情三处入口共用一个开关，
 * 抽屉本体只挂 App 层一份（单例），带对象的入口把上下文一并传入。
 */
export const usePilotStore = defineStore('pilot', () => {
  const open = ref(false)
  const game = ref<PilotGameContext | null>(null)

  function openPilot(context?: PilotGameContext | null) {
    game.value = context ?? null
    open.value = true
  }

  return { open, game, openPilot }
})
