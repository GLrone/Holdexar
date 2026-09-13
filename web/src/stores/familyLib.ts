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
 * 活跃度档位 → 词条 key。
 *
 * **只存 key、不存译文**：模块级常量在模块加载那一刻求值一次，存中文等于把语言
 * 冻死在那一刻，组件拿到的永远是中文（「模块级常量存译文」的冻结陷阱，只是发生
 * 在 store 里——brief6 D7）。译文由消费方在**渲染期** `t()` 现取。
 *
 * 属性名以 `Key` 结尾是有意的：`check-i18n.mjs` 的判据 ③ 只扫这种形状的常量表。
 * 这四个 key 的取用点是 `t(m.statusKey)`（动态调用），判据 ② 的静态字面量扫描
 * 看不见它——不写成这个形状，拼错 key 会一路进产物、运行时静默渲染成 key 原文。
 */
const STATUS_LABEL = {
  active: { statusKey: 'famInsight.status.active' },
  warm: { statusKey: 'famInsight.status.warm' },
  cold: { statusKey: 'famInsight.status.cold' },
  dormant: { statusKey: 'famInsight.status.dormant' },
} as const

/**
 * 家庭共享库共享数据源：family 页 7 个 tab 的统一真数据后端快照。
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
    // 已就绪且非强制刷新：直接复用。这个守卫原先只写在各 tab 组件里，
    // 而 family 首页的 onMounted 每次进入都会调一次 load()——不加这行，
    // 板块来回切就是一次次白等（后端快照兜底那 2s 的老路径）。
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
   * 入库日分布（对齐原版 computeAllHeatmaps 的 dayMap）：
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

  /**
   * 成员入库活跃度（对齐原版 _computeMemberActivityRaw）：
   * daysSinceLatest = 今天 - 该成员最后一次入库；分档 活跃<14/温热<60/冷淡<180/沉睡。
   * 含健康分（四档均值 100/75/50/25）与月均入库。
   *
   * 档位标签**只回 key**（`statusKey`），不回译文——store 里没有 t()，硬编码中文
   * 就是冻死。消费方 `FamInsights.vue` 在渲染期 `t(m.statusKey)` 现取。
   * `monthlyAvg` 回**数值**而不是 `.toFixed(1)` 的字符串：格式化统一走
   * `useLocaleFormat()` 出口，store 不替视图决定显示形态。
   */
  const memberActivity = computed(() => {
    const nowSec = Math.floor(Date.now() / 1000)
    const order = { active: 0, warm: 1, cold: 2, dormant: 3 } as const
    const scoreMap = { active: 100, warm: 75, cold: 50, dormant: 25 } as const
    const out = members.value.map((m) => {
      const times = acquiredGames.value
        .filter((g) => g.owners.includes(m.steamid))
        .map((g) => g.timeAcquired)
      const latest = times.length ? Math.max(...times) : -1
      const first = times.length ? Math.min(...times) : -1
      const days = latest > 0 ? Math.floor((nowSec - latest) / 86400) : -1
      const status = days < 0 || days >= 180 ? 'dormant' : days < 14 ? 'active' : days < 60 ? 'warm' : 'cold'
      const libMonths = first > 0 ? Math.max(1, (nowSec - first) / 2592000) : 1
      return {
        ...m,
        total: times.length,
        latestTime: latest,
        daysSinceLatest: days,
        status,
        statusKey: STATUS_LABEL[status].statusKey,
        monthlyAvg: times.length / libMonths,
      }
    })
    const healthScore = out.length
      ? Math.round(out.reduce((s, m) => s + scoreMap[m.status], 0) / out.length)
      : 0
    out.sort((a, b) => order[a.status] - order[b.status] || b.total - a.total)
    const counts = { active: 0, warm: 0, cold: 0, dormant: 0 }
    for (const m of out) counts[m.status]++
    return { members: out, healthScore, counts }
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
    memberActivity,
    fromSnapshot,
    load,
  }
})
