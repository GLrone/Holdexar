import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  achievementsApi,
  type AchievementNoteItem,
  type ChallengeClaimResult,
  type ChallengesPayload,
  type ComparePayload,
  type FeedItem,
  type LevelPayload,
  type NoteMood,
  type NoteWriteResult,
} from '@/api/client'

/** 最近解锁流的条数（后端上限 100） */
const FEED_LIMIT = 30

/**
 * 奖杯大厅共享数据源：猎手等级 / 挑战 / 批注墙 / 最近解锁 / 社交对比。
 *
 * **零本地推算**：曲线、段位、奖励数值、可领取判定全部消费后端字段——
 * 等级在后端是经验流水的纯派生，前端任何复算都会形成第二套口径。
 *
 * **账号隔离**：account 与 achievements / career store 同约定（'' = 主账号），
 * 请求一路带 steamid；切号清空载荷重拉。跨 store 接线（同步完成刷新、
 * 切号联动）由成就页 Index.vue 驱动，store 不反向依赖。
 *
 * 刷新语义：load() 进页加载（本会话已有数据即跳过，keep-alive 零请求）；
 * refresh() 强制绕缓存拉新（同步收尾、切号后首拉走这里）。claim / 批注
 * 写操作返回后端结果抛给调用方（组件做即时反馈），错误向上抛不吞；
 * 列表级读取失败记 error，不动已渲染数据。
 */
export const useTrophyHallStore = defineStore('trophyHall', () => {
  /** 当前账号（'' = 主账号，与 achievements store 同约定） */
  const account = ref('')

  const level = ref<LevelPayload | null>(null)
  const challenges = ref<ChallengesPayload | null>(null)
  const notes = ref<AchievementNoteItem[]>([])
  const feed = ref<FeedItem[]>([])
  const compare = ref<ComparePayload | null>(null)
  /** 社交对比当前勾选的账号（空 = 后端取可切换账号前 4 个） */
  const compareSteamids = ref<string[]>([])

  const loading = ref(false)
  const error = ref('')

  /** 本会话是否从网络取回过一版：防止水合后整个会话不再回源 */
  let fetchedThisSession = false

  async function reloadCore(): Promise<void> {
    const [levelData, challengesData, notesData] = await Promise.all([
      achievementsApi.level(account.value),
      achievementsApi.challenges(account.value),
      achievementsApi.notes(account.value),
    ])
    level.value = levelData
    challenges.value = challengesData
    notes.value = notesData.notes
  }

  /** 全量强制刷新（同步收尾驱动）：绕开 GET 时间窗缓存 */
  async function refresh(): Promise<void> {
    if (loading.value) return
    loading.value = true
    error.value = ''
    try {
      const [levelData, challengesData, notesData, feedData] = await Promise.all([
        achievementsApi.level(account.value, true),
        achievementsApi.challenges(account.value, true),
        achievementsApi.notes(account.value, true),
        achievementsApi.feed(FEED_LIMIT, account.value, true),
      ])
      level.value = levelData
      challenges.value = challengesData
      notes.value = notesData.notes
      feed.value = feedData.items
      fetchedThisSession = true
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /** 进页加载：本会话已有数据则跳过（吃 60s 时间窗缓存） */
  async function load(): Promise<void> {
    if (fetchedThisSession) return
    await refresh()
  }

  /** 切账号：清空全部载荷重拉（等级数据是后端派生的轻读取，不留快照） */
  async function setAccount(steamid: string): Promise<void> {
    if (steamid === account.value) return
    account.value = steamid
    level.value = null
    challenges.value = null
    notes.value = []
    feed.value = []
    compare.value = null
    compareSteamids.value = []
    fetchedThisSession = false
    error.value = ''
    await refresh()
  }

  /** 领取挑战奖励：返回后端结算结果（含涨级即时反馈）；写后拉新账本面 */
  async function claim(challengeId: string): Promise<ChallengeClaimResult> {
    const result = await achievementsApi.claimChallenge(challengeId, account.value)
    try {
      await reloadCore()
    } catch {
      /* 刷新失败不吞领取结果，下一拍刷新兜底 */
    }
    return result
  }

  /** 保存批注：仅首次写入给 XP（后端按幂等键判定）；批注数是评语挑战的进度来源 */
  async function saveNote(input: {
    appid: number
    imageName: string
    content: string
    mood?: NoteMood | null
  }): Promise<NoteWriteResult> {
    const result = await achievementsApi.addNote({ steamid: account.value, ...input })
    try {
      await reloadCore()
    } catch {
      /* 同上 */
    }
    return result
  }

  /** 删除批注（不存在静默；删除不回冲 XP） */
  async function removeNote(appid: number, imageName: string): Promise<void> {
    await achievementsApi.deleteNote(appid, imageName, account.value)
    notes.value = notes.value.filter((n) => !(n.appid === appid && n.imageName === imageName))
    try {
      // 批注数变化要反映到评语挑战进度（列表已在本地收敛，只拉挑战与等级）
      const [levelData, challengesData] = await Promise.all([
        achievementsApi.level(account.value),
        achievementsApi.challenges(account.value),
      ])
      level.value = levelData
      challenges.value = challengesData
    } catch {
      /* 同上 */
    }
  }

  /** 社交对比：steamids 缺省沿用上次选择，为空时后端取可切换账号前 4 个 */
  async function loadCompare(steamids?: string[]): Promise<void> {
    if (steamids) compareSteamids.value = steamids
    compare.value = await achievementsApi.compare(compareSteamids.value)
  }

  return {
    account,
    level,
    challenges,
    notes,
    feed,
    compare,
    compareSteamids,
    loading,
    error,
    load,
    refresh,
    setAccount,
    claim,
    saveNote,
    removeNote,
    loadCompare,
  }
})
