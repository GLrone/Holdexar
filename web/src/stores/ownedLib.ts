import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  watchPoolApi,
  type OwnedLibAccount,
  type OwnedLibGame,
  type OwnedLibraryPayload,
} from '@/api/client'
import { normalizeAvatarUrl } from '@/api/avatar'

/**
 * 游戏库（owned-library）共享数据源：gamelib 页「账号游戏库 / 库分析」两个
 * 页签的统一真数据快照。load() 失败记录 message（未绑账户 / 同步未跑 → 诚实
 * 空态，各页签自渲染）；load(true) 强制重拉。与 familyLib store 同款守卫：
 * ready + 非强制直接复用，页签来回切不白等。
 */
export const useOwnedLibStore = defineStore('ownedLib', () => {
  const data = ref<OwnedLibraryPayload | null>(null)
  const loading = ref(false)
  const error = ref('')

  const accounts = computed<OwnedLibAccount[]>(() =>
    (data.value?.accounts ?? []).map((a) => ({
      ...a,
      // 读出即归一（历史 CDN 域自愈，与 familyLib 同口径）
      avatarUrl: normalizeAvatarUrl(a.avatarUrl),
    })),
  )
  const games = computed<OwnedLibGame[]>(() => data.value?.games ?? [])
  const ready = computed(() => data.value !== null)

  /** steamid → 账户档案 */
  const accountMap = computed(() => {
    const map = new Map<string, OwnedLibAccount>()
    for (const a of accounts.value) map.set(a.steamid, a)
    return map
  })

  /** 账户显示名：备注名 → Steam 昵称 → steamid 尾号（后端没给时的兜底） */
  function accountName(steamid: string): string {
    const a = accountMap.value.get(steamid)
    return a?.label || a?.personaName || steamid.slice(-4)
  }

  /** 账户头像 URL（空串 → 展示层落首字符占位） */
  function accountAvatar(steamid: string): string {
    return accountMap.value.get(steamid)?.avatarUrl || ''
  }

  async function load(force = false) {
    if (loading.value) return
    if (ready.value && !force) return
    loading.value = true
    error.value = ''
    try {
      data.value = await watchPoolApi.ownedLibrary()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      if (!data.value) data.value = null
    } finally {
      loading.value = false
    }
  }

  return {
    data,
    loading,
    error,
    ready,
    accounts,
    games,
    accountMap,
    accountName,
    accountAvatar,
    load,
  }
})
