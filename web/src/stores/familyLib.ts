import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  familyApi,
  type FamilyLibGame,
  type FamilyLibMember,
  type FamilyLibraryPayload,
  type FamilyMemberPlayEntry,
} from '@/api/client'
import { normalizeAvatarUrl } from '@/api/avatar'

/**
 * 家庭共享库共享数据源：family 页各 tab 的统一真数据后端快照。
 * load() 失败时记录 message（未绑 Cookie / 未加入家庭组 → 诚实引导），
 * tabs 各自渲染空态；refresh() 强制重拉（后端绕过 5 分钟缓存）。
 */
export const useFamilyStore = defineStore('familyLib', () => {
  const data = ref<FamilyLibraryPayload | null>(null)
  const loading = ref(false)
  const error = ref('')

  // 成员头像 URL 就地归一（旧快照可能是已死/轮换 CDN 域）——7 个 tab 同源
  const members = computed<FamilyLibMember[]>(() =>
    (data.value?.members ?? []).map((m) => ({ ...m, avatarUrl: normalizeAvatarUrl(m.avatarUrl) })),
  )
  const games = computed<FamilyLibGame[]>(() => data.value?.games ?? [])
  const memberPlay = computed<Record<string, FamilyMemberPlayEntry[]>>(
    () => data.value?.memberPlay ?? {},
  )
  const ready = computed(() => data.value !== null)

  /** steamid → 成员档案（含首字回退头像名） */
  const memberMap = computed(() => {
    const map = new Map<string, FamilyLibMember>()
    for (const m of members.value) map.set(m.steamid, m)
    return map
  })

  function memberName(steamid: string): string {
    const m = memberMap.value.get(steamid)
    return m?.personaName || steamid.slice(-4)
  }

  /** 独占 = 拥有者仅 1 人（含共享清单 presence=1） */
  const exclusiveGames = computed(() => games.value.filter((g) => g.ownerCount === 1))
  const sharedGames = computed(() => games.value.filter((g) => g.ownerCount > 1))

  /** 成员独占贡献数 + 库价值（CN 价合计） */
  const memberStats = computed(() =>
    members.value.map((m) => {
      const owned = games.value.filter((g) => g.owners.includes(m.steamid))
      const exclusive = owned.filter((g) => g.ownerCount === 1)
      const valueFen = owned.reduce((s, g) => s + (g.cnPriceFen ?? 0), 0)
      const play = memberPlay.value[m.steamid] ?? []
      const totalMin = play.reduce((s, p) => s + p.minutes, 0)
      const recent2wMin = play.reduce((s, p) => s + p.minutes2w, 0)
      return {
        ...m,
        ownedCount: owned.length || m.ownedCount,
        exclusiveCount: exclusive.length,
        valueFen,
        playtimeTotalMin: totalMin,
        playtime2wMin: recent2wMin,
      }
    }),
  )

  const libraryKpi = computed(() => {
    const totalMin = games.value.reduce((s, g) => s + g.playtimeMinutes, 0)
    const valueFen = games.value.reduce((s, g) => s + (g.cnPriceFen ?? 0), 0)
    const pricedCount = games.value.filter((g) => g.cnPriceFen !== null).length
    const nowSec = Math.floor(Date.now() / 1000)
    const active30 = games.value.filter(
      (g) => g.lastPlayed > 0 && g.lastPlayed > nowSec - 30 * 86400,
    ).length
    return {
      total: games.value.length,
      exclusive: exclusiveGames.value.length,
      shared: sharedGames.value.length,
      active30,
      totalMin,
      valueFen,
      pricedCount,
    }
  })

  async function load(force = false) {
    if (loading.value) return
    // 已就绪且非强制刷新：直接复用。守卫必须在这一层（各 tab 组件只写自己那份），
    // 因为 family 首页的 onMounted 每次进入都会调一次 load()——不加这行，板块
    // 来回切就是一次次白等（后端快照兜底那 2s 的路径）。
    if (ready.value && !force) return
    loading.value = true
    error.value = ''
    try {
      data.value = force ? await familyApi.refreshLibrary() : await familyApi.library()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      if (!data.value) data.value = null
    } finally {
      loading.value = false
    }
  }

  /* ── 入库时间口径（rt_time_acquired）——热力图/增长趋势/购买动态共用 ── */

  const acquiredGames = computed(() =>
    games.value.filter((g) => g.timeAcquired > 0),
  )

  /** 快照兜底标记（实时聚合失败时后端回快照数据） */
  const fromSnapshot = computed(() => data.value?.fromSnapshot === true)

  /**
   * 入库日分布（family 与成员各自一份 dayMap）：
   * family = { 'YYYY-MM-DD': 入库数 }；members = { steamid → 同构 dayMap }
   */
  const acquiredDayMaps = computed(() => {
    const family: Record<string, number> = {}
    const perMember: Record<string, Record<string, number>> = {}
    for (const sid of members.value.map((m) => m.steamid)) perMember[sid] = {}
    for (const g of acquiredGames.value) {
      const d = new Date(g.timeAcquired * 1000)
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
      family[key] = (family[key] ?? 0) + 1
      for (const sid of g.owners) {
        if (perMember[sid]) perMember[sid]![key] = (perMember[sid]![key] ?? 0) + 1
      }
    }
    return { family, members: perMember }
  })

  /** 入库月度聚合（增长趋势折线）：[key]=YYYY-MM → { all, members: {sid:n} }，按月升序 */
  const monthlyAcquired = computed(() => {
    const map = new Map<string, { all: number; members: Record<string, number> }>()
    for (const g of acquiredGames.value) {
      const d = new Date(g.timeAcquired * 1000)
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
      if (!map.has(key)) map.set(key, { all: 0, members: {} })
      const entry = map.get(key)!
      entry.all++
      for (const sid of g.owners) entry.members[sid] = (entry.members[sid] ?? 0) + 1
    }
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  })

  return {
    data,
    loading,
    error,
    ready,
    members,
    games,
    memberPlay,
    memberMap,
    memberName,
    memberStats,
    exclusiveGames,
    sharedGames,
    libraryKpi,
    acquiredGames,
    acquiredDayMaps,
    monthlyAcquired,
    fromSnapshot,
    load,
  }
})
