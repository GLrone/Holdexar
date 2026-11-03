import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

import { ownershipApi, type OwnershipInfo } from '@/api/client'
import { useCrawlStatusStore } from '@/stores/crawlStatus'

/**
 * 游戏归属（游戏卡左上角状态徽章数据源：已拥有/家庭共享/愿望单 + 归属账号）。
 * 卡片按需 ensure(appid)，同一帧内的请求合并为一次批量拉取（一页 40 张卡 = 1 个请求），
 * 结果常驻缓存；账户同步是任务账本里的 job，其终态推进 dataEpoch——纪元一推进即整批
 * 重取原缓存清单，徽章响应式跟随。
 */
export const useOwnershipStore = defineStore('ownership', () => {
  const map = ref<Record<number, OwnershipInfo>>({})

  const pending = new Set<number>()
  const inflight = new Set<number>()
  let flushTimer: number | null = null

  async function flush() {
    flushTimer = null
    const appids = [...pending].filter((id) => !inflight.has(id))
    pending.clear()
    if (appids.length === 0) return
    appids.forEach((id) => inflight.add(id))
    try {
      const res = await ownershipApi.batch(appids)
      const next = { ...map.value }
      for (const [appid, info] of Object.entries(res.ownerships)) {
        next[Number(appid)] = info
      }
      map.value = next
    } catch {
      /* 静默：拉取失败时卡片不显示徽章，下次挂载重试 */
    } finally {
      appids.forEach((id) => inflight.delete(id))
    }
  }

  /** 卡片挂载时调用；合并同帧请求，约一帧后统一批量拉取 */
  function ensure(appid: number) {
    if (map.value[appid] || pending.has(appid) || inflight.has(appid)) return
    pending.add(appid)
    if (flushTimer === null) {
      flushTimer = window.setTimeout(flush, 32)
    }
  }

  /** 账户数据任务终态后的整批重取：清空缓存并按原清单重拉（徽章响应式跟随） */
  function refresh() {
    const ids = Object.keys(map.value).map(Number)
    map.value = {}
    ids.forEach((id) => ensure(id))
  }

  const crawl = useCrawlStatusStore()
  watch(
    () => crawl.dataEpoch,
    () => {
      if (Object.keys(map.value).length > 0) refresh()
    },
  )

  return { map, ensure, refresh }
})
