/* ════════════════════════════════════════════════════════════════════
   island.ts — 灵动岛兼容桥接层
   保持既有命令式接口（pushNotice, beginTask, updateTask, endTask）
   与响应式 island 状态完全不变，底层统一桥接至 ActivityManager。
   ════════════════════════════════════════════════════════════════════ */
import { reactive } from 'vue'
import { activityManager } from './activity/activityManager.ts'
import { islandController } from './activity/islandController.ts'

/** 消息语义：四种结果态 + 进行中态 */
export type IslandTone = 'success' | 'error' | 'warning' | 'info' | 'progress'

export interface IslandNotice {
  id: number
  tone: IslandTone
  /** 收起态单行文案 */
  text: string
  /** 展开态正文；留空时展开面只显示文案与动作 */
  detail: string
  /** 进行中项：不自动消失，结果消息到达时顶替 */
  sticky: boolean
  /** 自动收起延时（ms）；sticky 与 0 都不自动消失 */
  duration: number
  /** 失败项的原地重试回调 */
  retry: (() => void) | null
  /** 详情动作的跳转目标路由 */
  to: string
  /** 详情动作的按钮文案键；留空时按通用文案渲染 */
  toLabel: string
  /** 单款游戏事实可携带封面图；多个游戏时留空 */
  iconImageUrl?: string
}

export interface IslandState {
  notices: IslandNotice[]
  /** 全局长任务（store 发起，跨页存活）：进行中的任务注册表，新任务在首 */
  tasks: IslandTask[]
  /** 展开详情面 */
  expanded: boolean
  /** 指针停留在岛内，队列倒计时在此期间挂起 */
  hovered: boolean
}

/** 全局长任务：进度行与完成情况由发起方 store 持续喂养，结束即撤下 */
export interface IslandTask {
  /** 发起方约定的唯一键（如 'bills-sync'）；重复 begin 就地覆盖 */
  key: string
  /** 进度行（发起方在更新时点现取译文，轮询节拍下语言切换自然跟上） */
  text: string
  /** 定量进度 0-100；null 走滑动条（后端不预告总量时） */
  percent: number | null
  /** 「查看」动作的落点路由；留空不出该动作 */
  to: string
}

/** 队列上限：只保留最近若干条，倒计时推进不受阻塞 */
const QUEUE_MAX = 4

export const island = reactive<IslandState>({
  notices: [],
  tasks: [],
  expanded: false,
  hovered: false,
})

let seq = 0
let timer: ReturnType<typeof setTimeout> | null = null

function clearTimer() {
  if (timer !== null) {
    clearTimeout(timer)
    timer = null
  }
}

/** 当前展示项的就地移除 */
export function dropNotice(id: number) {
  const at = island.notices.findIndex((n) => n.id === id)
  if (at >= 0) {
    island.notices.splice(at, 1)
  }
  activityManager.remove(`notice:${id}`)
  armTimer()
}

/** 队首展示项的存活倒计时：队首更替即重排，悬停与展开期间挂起 */
export function armTimer() {
  clearTimer()
  const head = island.notices[0]
  if (!head || head.sticky || !head.duration) return
  if (island.expanded || island.hovered) return
  timer = setTimeout(() => {
    const expired = island.notices.shift()
    if (expired) {
      activityManager.remove(`notice:${expired.id}`)
    }
    armTimer()
  }, head.duration)
}

/** 入队一条消息，返回其 id */
export function pushNotice(notice: Omit<IslandNotice, 'id'>): number {
  /* 进行中与结果共用同一个展示槽位：两者的在队项各自只留一条 */
  const stickyAt = island.notices.findIndex((n) => n.sticky)
  if (notice.sticky) {
    if (stickyAt >= 0) {
      const removed = island.notices.splice(stickyAt, 1)[0]
      if (removed) activityManager.remove(`notice:${removed.id}`)
    }
  } else if (stickyAt >= 0) {
    const removed = island.notices.splice(stickyAt, 1)[0]
    if (removed) activityManager.remove(`notice:${removed.id}`)
  }

  const item: IslandNotice = { ...notice, id: (seq += 1) }
  island.notices.unshift(item)
  if (island.notices.length > QUEUE_MAX) {
    const dropped = island.notices.splice(QUEUE_MAX)
    for (const d of dropped) {
      activityManager.remove(`notice:${d.id}`)
    }
  }

  // 同步桥接至 ActivityManager
  activityManager.register({
    id: `notice:${item.id}`,
    source: 'notice',
    kind: 'notice',
    status: item.tone === 'error' ? 'failed' : (item.sticky ? 'running' : 'success'),
    title: item.text,
    detail: item.detail,
    iconImageUrl: item.iconImageUrl,
    tone: item.tone,
    duration: item.sticky ? 0 : item.duration,
    retry: item.retry,
    to: item.to,
    toLabel: item.toLabel,
    needsAttention: item.tone === 'error',
  })

  armTimer()
  return item.id
}

/** 更新在队项的文案（进行中的进度文本走这里） */
export function updateNotice(id: number, text: string) {
  const item = island.notices.find((n) => n.id === id)
  if (item) {
    item.text = text
    activityManager.update(`notice:${id}`, { title: text })
  }
}

/** 展开面的开合；收起后恢复倒计时 */
export function setExpanded(on: boolean) {
  island.expanded = on
  islandController.setExpanded(on)
  armTimer()
}

/* ── 全局长任务注册层 ──
   store 发起的长任务跨页存活，进度与完成情况在此维护并桥接至 ActivityManager */

/** 注册（或就地刷新）一个进行中任务；同 key 重复注册视为续接 */
export function beginTask(key: string, text: string, opts?: { percent?: number | null; to?: string }) {
  const at = island.tasks.findIndex((task) => task.key === key)
  const task: IslandTask = { key, text, percent: opts?.percent ?? null, to: opts?.to ?? '' }
  if (at >= 0) island.tasks.splice(at, 1)
  island.tasks.unshift(task)

  const sourceDomain = key.split('-')[0] || 'task'
  activityManager.register({
    id: `task:${key}`,
    source: sourceDomain,
    kind: 'task',
    status: 'running',
    title: text,
    progress: task.percent,
    to: task.to,
    needsAttention: false,
  })
}

/** 更新在跑任务的进度行与定量进度 */
export function updateTask(key: string, text: string, percent?: number | null) {
  const task = island.tasks.find((t) => t.key === key)
  if (!task) return
  task.text = text
  if (percent !== undefined) task.percent = percent

  activityManager.update(`task:${key}`, {
    title: text,
    ...(percent !== undefined ? { progress: percent } : {}),
  })
}

/** 任务收场：撤下展示位 */
export function endTask(key: string) {
  const at = island.tasks.findIndex((task) => task.key === key)
  if (at >= 0) island.tasks.splice(at, 1)
  activityManager.remove(`task:${key}`)
}

/** 悬停态切换；进入即挂起倒计时，离开即恢复 */
export function setHovered(on: boolean) {
  island.hovered = on
  islandController.setHovered(on)
  armTimer()
}
