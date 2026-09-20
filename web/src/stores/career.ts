/** 游戏生涯数据（成就殿堂的「生涯侧写」全部子模块共用一次读取）。
 *
 *  数据放在页面级 store：页面按「概括 → 明细」递增重排，生涯的八块与成就
 *  四节**交错**落位（纪录殿堂插在亮点之前…），挂在任何单个 CareerPanel 的
 *  局部状态都撑不起这种交错。
 *
 *  刷新时机：挂载时拉一次；`achievements.syncing` 从 true 落到 false（同步跑完、
 *  成就库变了）时自动重算一版；用户点「重新推导」则绕开 60s 时间窗缓存。
 *
 *  账号隔离：生涯载荷同样按账号分键存快照（切号先水合该账号自己的旧数据），
 *  请求一路带 steamid——后端每个度量都按 steamid 聚合。
 */
import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

import { careerApi, type CareerPayload } from '@/api/client'
import { readSnapshot, writeSnapshot } from '@/lib/localSnapshot'

/** 快照键与版本：载荷结构变更时 +1（旧结构直接丢弃，不喂给新组件） */
const SNAP = { key: 'achievements.career', version: 1 }
const SNAP_MAX_AGE = 24 * 60 * 60 * 1000

/** 按账号分键：'' 表示主账号（与 achievements store 同一约定） */
function snapKey(steamid: string): string {
  return steamid ? `${SNAP.key}.${steamid}` : SNAP.key
}

export const useCareerStore = defineStore('career', () => {
  /* 当前账号（页面在切号时同步过来） */
  const account = ref('')

  /* 同步水合上一次的生涯载荷：十几个分节全靠它渲染，等接口回来才画首帧
     就是"切过去卡两三秒"的主因之一。先给旧数据，回包再静默替换。 */
  const career = ref<CareerPayload | null>(
    readSnapshot<CareerPayload>(snapKey(''), SNAP.version, SNAP_MAX_AGE),
  )
  const loading = ref(false)
  const failed = ref(false)
  const refreshing = ref(false)
  /* 本会话是否真的从网络取回过：快照只负责"首帧不空"，**不能**当成已加载——
     否则水合一次后整个会话都不再回源，最长要顶 24 小时（快照上限）的旧账。 */
  let fetchedThisSession = false
  let attached = false

  async function load(fresh = false, steamid = account.value): Promise<void> {
    account.value = steamid
    // 本会话已取回且非强制 → 跳过（keep-alive 重进零开销；刷新由同步收尾驱动）。
    // 只有快照、没有回源 → 继续走网络：渲染不受影响（快照已在 ref 里），
    // 回包只是静默替换，所以"有内容"与"数据新鲜"这两件事互不牺牲。
    if (!fresh && fetchedThisSession) return
    if (fresh) refreshing.value = true
    else if (!career.value) loading.value = true
    failed.value = false
    try {
      career.value = await careerApi.career(fresh, steamid)
      fetchedThisSession = true
      writeSnapshot(snapKey(steamid), SNAP.version, career.value)
    } catch {
      // 生涯是只读聚合，失败不该把整页打成错误页：保留旧数据，只标记失败
      failed.value = true
    } finally {
      loading.value = false
      refreshing.value = false
    }
  }

  /** 切账号：换快照键并水合该账号自己的旧载荷，标记需回源 */
  function setAccount(steamid: string): void {
    if (steamid === account.value) return
    account.value = steamid
    fetchedThisSession = false
    failed.value = false
    career.value = readSnapshot<CareerPayload>(snapKey(steamid), SNAP.version, SNAP_MAX_AGE)
  }

  /** 只挂一次：watch 同步态 → 同步跑完自动重算 */
  function attach(syncing: () => boolean): void {
    if (attached) return
    attached = true
    watch(syncing, (now, was) => {
      if (was && !now) void load(true)
    })
  }

  /** 有凭证且有统计口径的数据才渲染八块；空库渲染全是空态，反而更吵 */
  function hasData(): boolean {
    const c = career.value
    return !!c && (c.playtime.totalMin > 0 || c.trophy.total > 0)
  }

  return { account, career, loading, failed, refreshing, load, setAccount, attach, hasData }
})