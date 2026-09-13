import { defineStore } from 'pinia'
import { ref } from 'vue'

import { followsApi } from '@/api/client'

/**
 * 关注列表（游戏卡星标数据源 = 后端追踪池 manual 条目，星标是关注的唯一入口：
 * 关注即入池，爬取队列最优先；导入通道不产生关注）。首次用到时整表拉一次——
 * 关注集是用户手工策展的小集合，单请求足够；切换走乐观更新，失败回滚。
 */
export const useFollowsStore = defineStore('follows', () => {
  const ids = ref<Set<number>>(new Set())
  const loaded = ref(false)
  let inflight: Promise<void> | null = null

  /** 首次调用整表拉取；并发共享同一请求，失败静默（下次挂载重试） */
  function ensure(): Promise<void> {
    if (loaded.value) return Promise.resolve()
    if (!inflight) {
      inflight = followsApi
        .list()
        .then((res) => {
          ids.value = new Set(res.appids)
          loaded.value = true
        })
        .catch(() => {
          /* 静默：星标缺席好过误亮 */
        })
        .finally(() => {
          inflight = null
        })
    }
    return inflight
  }

  function has(appid: number): boolean {
    return ids.value.has(appid)
  }

  /**
   * 切换关注（乐观更新，失败回滚并向上抛错——提示由调用方负责，那里才有
   * i18n 语境与 message 出口）。服务端语义：关注入追踪池，取消只清 manual 标。
   */
  async function toggle(appid: number): Promise<void> {
    const was = ids.value.has(appid)
    if (was) ids.value.delete(appid)
    else ids.value.add(appid)
    try {
      await (was ? followsApi.remove(appid) : followsApi.add(appid))
    } catch (e) {
      if (was) ids.value.add(appid)
      else ids.value.delete(appid)
      throw e
    }
  }

  return { ids, loaded, ensure, has, toggle }
})
