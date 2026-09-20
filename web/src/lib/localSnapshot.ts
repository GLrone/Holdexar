/** 本地快照：把上一次成功的接口响应落到 localStorage，进页面时**同步**水合，
 *  先渲染旧数据、再后台校验（stale-while-revalidate）。
 *
 *  成就殿堂是「一次请求喂满十几个分节」的重页面，进入时要等接口回来才肯
 *  渲染首帧——接口本身不慢，但一次网络往返叠上前端首屏渲染，用户读作
 *  「切过去卡两三秒」。有了快照，切进来第一帧就有内容，网络回包只做静默替换。
 *
 *  几条纪律：
 *  · **同步读**：`readSnapshot` 必须是同步的，否则起不到"首帧即有内容"的作用；
 *  · **带版本与时间**：结构变了（version 升）直接丢弃，避免旧结构喂给新组件；
 *  · **过旧即弃**：超过 maxAge 就当没有，宁可等一次网络，也不展示可能误导的旧账；
 *  · **静默失败**：localStorage 可能被禁用/写满/坏数据，一律 try/catch 吞掉——
 *    快照只是加速手段，任何异常都不能把主流程拖下水。
 */

const PREFIX = 'holdexar.snapshot.'

interface Envelope<T> {
  v: number
  ts: number
  data: T
}

/** 同步读快照（无/过期/版本不符/坏数据 → null） */
export function readSnapshot<T>(key: string, version: number, maxAgeMs: number): T | null {
  try {
    const raw = localStorage.getItem(PREFIX + key)
    if (!raw) return null
    const env = JSON.parse(raw) as Envelope<T>
    if (!env || env.v !== version || typeof env.ts !== 'number') return null
    if (Date.now() - env.ts > maxAgeMs) return null
    return env.data
  } catch {
    return null
  }
}

/** 写快照（超配额/被禁用等一律静默） */
export function writeSnapshot<T>(key: string, version: number, data: T): void {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify({ v: version, ts: Date.now(), data }))
  } catch {
    /* 快照只是加速手段，写不进去不影响功能 */
  }
}

/** 快照年龄（ms）；无快照返回 -1（页面上用来提示"数据截至何时"） */
export function snapshotAge(key: string): number {
  try {
    const raw = localStorage.getItem(PREFIX + key)
    if (!raw) return -1
    const env = JSON.parse(raw) as Envelope<unknown>
    return typeof env?.ts === 'number' ? Date.now() - env.ts : -1
  } catch {
    return -1
  }
}