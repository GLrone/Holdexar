import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  achievementsApi,
  ApiError,
  type AchievementAccount,
  type AchievementGameFilter,
  type AchievementGameItem,
  type AchievementGameSort,
  type AchievementSummary,
  type AchievementSyncSnapshot,
} from '@/api/client'
import { readSnapshot, writeSnapshot } from '@/lib/localSnapshot'

/**
 * 成就殿堂共享数据源：summary KPI + 游戏列表 + 同步生命周期 + 账号选择。
 *
 * **游戏列表一次拉全量、筛选/排序/搜索全在本地算**：整表 450+ 行约 130KB，
 * 本地 SQLite 聚合 20ms 出参；而每次切分类都发请求的话，切换要等一个网络
 * 往返（空窗看着像卡住）。列表数据本就是本地落表的，一次取回后切换是纯
 * computed，零延迟。
 *
 * **同步态放 store**：同步是后端 fire-and-forget 任务（可达数分钟），
 * 组件一离开页面就卸载——放组件里必然「切走再回来进度没了」。store 是
 * pinia 单例、跨页存活：进页时 attach() 对齐后端 running 态接着轮询，
 * 与 updater store 同一套语义。
 *
 * **账号隔离**：所选账号（`account`，空 = 主账号）一路带进每个请求；后端按
 * steamid 落库与读取，前端这边则连本地快照都按账号分键——否则切号首帧会拿
 * 另一个账号的旧 KPI 顶上（看着像数据串号）。同步状态带 `steamid`，只认属于
 * 当前所选账号的那一份进度。
 *
 * 首次进入自动同步：summary.lastSyncedAt 为空且凭证可用时自动发起一次
 * （会话内只补一次，失败不重试——失败原因由空态文案给出路）。
 */
export const useAchievementsStore = defineStore('achievements', () => {
  /* 本地快照：进页面**同步**水合上一次的汇总，首帧就有内容，网络回包再静默替换。
     版本号在结构变更时 +1（旧结构直接丢弃，不喂给新组件）。 */
  const SNAPSHOT = { summary: 'achievements.summary', version: 1 }
  const SNAPSHOT_MAX_AGE = 24 * 60 * 60 * 1000

  /** 所选账号（'' = 主账号；按账号分键存本地，切号首帧不串数据） */
  const account = ref<string>(
    readSnapshot<string>('achievements.account', 1, SNAPSHOT_MAX_AGE) ?? '',
  )

  function summaryKey(): string {
    return account.value ? `${SNAPSHOT.summary}.${account.value}` : SNAPSHOT.summary
  }

  const accounts = ref<AchievementAccount[]>([])

  const summary = ref<AchievementSummary | null>(
    readSnapshot<AchievementSummary>(summaryKey(), SNAPSHOT.version, SNAPSHOT_MAX_AGE),
  )
  /** 全量游戏行（一次取回；筛选/排序/搜索都在它上面本地算） */
  const allGames = ref<AchievementGameItem[]>([])
  const loading = ref(false)
  const error = ref('')

  const filter = ref<AchievementGameFilter>('trophy')
  const sort = ref<AchievementGameSort>('recent')
  const search = ref('')

  /* 本地筛选/排序（比较器与后端 list_games 保持一致，口径不漂） */
  const games = computed<AchievementGameItem[]>(() => {
    const needle = search.value.trim().toLowerCase()
    const out = allGames.value.filter((g) => {
      if (filter.value === 'trophy' && g.total <= 0) return false
      if (filter.value === 'platinum' && !g.platinum) return false
      if (filter.value === 'progress' && !(g.unlocked > 0 && g.unlocked < g.total)) return false
      if (filter.value === 'external' && g.owned) return false
      if (needle && !g.name.toLowerCase().includes(needle)) return false
      return true
    })
    const byName = (a: AchievementGameItem, b: AchievementGameItem) =>
      a.name.toLowerCase().localeCompare(b.name.toLowerCase())
    if (sort.value === 'playtime') out.sort((a, b) => b.playtimeMin - a.playtimeMin || byName(a, b))
    else if (sort.value === 'progress') out.sort((a, b) => b.percent - a.percent || b.total - a.total || byName(a, b))
    else if (sort.value === 'recent') out.sort((a, b) => b.lastPlayed - a.lastPlayed || byName(a, b))
    else out.sort(byName)
    return out
  })

  const sync = ref<AchievementSyncSnapshot | null>(null)
  /** 本次会话是否已自动发起过首次同步 */
  const kicked = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null

  /** 同步状态只在「状态属于当前所选账号」时可见：切号后不该显示别人的进度 */
  const activeSync = computed<AchievementSyncSnapshot | null>(() => {
    const snap = sync.value
    if (!snap) return null
    // 空串 = 主账号：用账号清单里的 isPrimary 反查真实 steamid 再比对
    const target =
      account.value || accounts.value.find((a) => a.isPrimary)?.steamid || ''
    if (target && snap.steamid && snap.steamid !== target) return null
    return snap
  })

  /** 是否正在同步（派生自当前账号的同步快照，避免两处状态各说各话） */
  const syncing = computed(() => !!activeSync.value?.running)

  function stopPolling(): void {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  async function pollStatus(): Promise<void> {
    try {
      const snap = await achievementsApi.syncStatus()
      sync.value = snap
      if (snap.running) return
      stopPolling()
      // 同步收尾：数据变了，重拉 summary 与列表（含生涯，由页面 watch 驱动）
      await load(true)
    } catch {
      /* 轮询失败：交给下一拍，不中断页面 */
    }
  }

  function startPolling(): void {
    stopPolling()
    timer = setInterval(() => void pollStatus(), 1200)
  }

  /** 与后端对齐（进页调用）：续上别人（定时任务/上一会话）发起的同步 */
  async function attach(): Promise<void> {
    try {
      const snap = await achievementsApi.syncStatus()
      sync.value = snap
      if (snap.running) startPolling()
    } catch {
      /* 拉不到进度不影响页面其余部分 */
    }
  }

  /** 可切换账号清单（已绑账号 + 家庭成员；失败不阻断页面） */
  async function loadAccounts(): Promise<void> {
    try {
      const payload = await achievementsApi.accounts()
      accounts.value = payload.accounts
    } catch {
      accounts.value = []
    }
  }

  /** 取全量游戏行（排序交给本地 computed；后端排序参数仅供其它调用方） */
  async function refreshGames(): Promise<void> {
    const payload = await achievementsApi.games('all', 'name', '', account.value)
    allGames.value = payload.games
  }

  async function load(force = false): Promise<void> {
    if (loading.value) return
    // keep-alive 重进时数据已在内存且非强制 → 直接用现值（切回零请求零重渲）。
    // 数据刷新由同步收尾（pollStatus → load(true)）与手动同步驱动。
    if (!force && summary.value && allGames.value.length > 0) return
    loading.value = true
    error.value = ''
    try {
      summary.value = await achievementsApi.summary(account.value)
      writeSnapshot(summaryKey(), SNAPSHOT.version, summary.value)
      await refreshGames()
      // 首次进入自动同步：有凭证但从未同步过（会话内只补一次）
      if (
        !kicked.value &&
        !syncing.value &&
        summary.value.hasCredential &&
        !summary.value.lastSyncedAt
      ) {
        kicked.value = true
        void startSync()
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** 切账号：清掉上一个账号的内存态与首帧快照，重拉当前账号 */
  async function setAccount(steamid: string): Promise<void> {
    if (steamid === account.value) return
    account.value = steamid
    writeSnapshot('achievements.account', 1, steamid)
    stopPolling()
    error.value = ''
    // 快照按账号分键：切过去先水合该账号自己的旧数据（没有就空着等网络）
    summary.value = readSnapshot<AchievementSummary>(
      summaryKey(),
      SNAPSHOT.version,
      SNAPSHOT_MAX_AGE,
    )
    allGames.value = []
    await load(true)
  }

  /** 发起同步；409（已在进行中）视为续看进度而非失败 */
  async function startSync(): Promise<void> {
    try {
      const snap = await achievementsApi.sync(account.value)
      sync.value = snap
      if (snap.running) startPolling()
    } catch (e) {
      // 已在进行中（409）：转轮询续看（先占位写上本轮目标账号，免得进度被判成别人的）
      if (e instanceof ApiError && e.status === 409) {
        sync.value = { running: true, steamid: account.value }
        startPolling()
        return
      }
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  /* 三个 setter 只改状态：列表是本地 computed，切换即时生效（无网络往返） */
  function setFilter(v: AchievementGameFilter): void {
    filter.value = v
  }

  function setSort(v: AchievementGameSort): void {
    sort.value = v
  }

  function setSearch(v: string): void {
    search.value = v
  }

  return {
    account,
    accounts,
    summary,
    allGames,
    games,
    loading,
    error,
    filter,
    sort,
    search,
    sync,
    activeSync,
    syncing,
    kicked,
    load,
    loadAccounts,
    setAccount,
    attach,
    startSync,
    refreshGames,
    setFilter,
    setSort,
    setSearch,
    stopPolling,
  }
})