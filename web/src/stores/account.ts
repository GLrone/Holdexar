import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { accountApi, type AccountStatus, type SteamAccountItem } from '@/api/client'
import { normalizeAvatarUrl } from '@/api/avatar'

/**
 * Steam 多账户绑定与钱包余额（顶栏胶囊 + 设置页「我」共享）。
 * load() 只读快照不触发抓取；sync() 强制向 Steam 抓一次（当前账号）。
 *
 * 多账号语义：主账号 = accounts[0]（第一个绑定的，愿望单/家庭/账单跟随）；
 * 当前账号（active）= is_active 的操作账号（钱包展示/CDK 激活/免费领取跟随）。
 */
export const useAccountStore = defineStore('account', () => {
  const status = ref<AccountStatus | null>(null)
  const syncing = ref(false)
  const loaded = ref(false)
  const switching = ref(false)

  /** 多账号列表（绑定顺序） */
  const accounts = computed<SteamAccountItem[]>(
    () => status.value?.accounts ?? [],
  )
  /** 主账号（第一个绑定的；未绑定为 null） */
  const primary = computed<SteamAccountItem | null>(
    () => accounts.value.find((a) => a.is_primary) ?? null,
  )
  /** 当前操作账号（active；数据异常时回退主账号） */
  const active = computed<SteamAccountItem | null>(
    () => accounts.value.find((a) => a.is_active) ?? primary.value,
  )

  function apply(next: AccountStatus) {
    // 头像 URL 统一出口：profile + 全部账号就地归一（顶栏/钱包行/设置页同源）
    const s: AccountStatus = {
      ...next,
      accounts: (next.accounts ?? []).map((a) => ({ ...a, avatar_url: normalizeAvatarUrl(a.avatar_url) })),
    }
    if (s.profile) s.profile = { ...s.profile, avatar_url: normalizeAvatarUrl(s.profile.avatar_url) }
    status.value = s
    loaded.value = true
  }

  async function load() {
    if (syncing.value) return
    try {
      apply(await accountApi.status())
    } catch {
      /* 静默：顶栏缺余额胶囊不致命 */
    }
  }

  async function sync() {
    if (syncing.value) return
    syncing.value = true
    try {
      apply(await accountApi.sync())
    } catch (e) {
      // 同步失败也保留旧快照，只把错误挂到当前状态
      if (status.value) status.value = { ...status.value, sync_error: String(e) }
    } finally {
      syncing.value = false
    }
  }

  async function bindCookies(cookies: string) {
    apply(await accountApi.bindCookies(cookies))
  }

  /** 切换当前账号（钱包/CDK/免费领取跟随） */
  async function setActive(steamId: string) {
    if (switching.value) return
    switching.value = true
    try {
      apply(await accountApi.setActive(steamId))
    } finally {
      switching.value = false
    }
  }

  /** 删除指定账号（删除 active 时后端自动回退剩余首个） */
  async function removeAccount(steamId: string) {
    apply(await accountApi.removeAccount(steamId))
  }

  async function unbind() {
    apply(await accountApi.unbind())
  }

  return {
    status, syncing, loaded, switching,
    accounts, primary, active,
    load, sync, bindCookies, setActive, removeAccount, unbind,
  }
})
