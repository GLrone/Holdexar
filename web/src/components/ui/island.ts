/* ════════════════════════════════════════════════════════════════════
   island.ts — 灵动岛状态层
   消息队列与展示态的唯一事实来源：message.ts 命令式推入，
   HlIsland.vue 响应式读取。
   notices[0] 为当前展示项：新消息入队首，进行中项由结果消息就地顶替。
   ════════════════════════════════════════════════════════════════════ */
import { reactive } from 'vue'

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
}

export interface IslandState {
  notices: IslandNotice[]
  /** 全局长任务（store 发起，跨页存活）：进行中的任务注册表，新任务在首 */
  tasks: IslandTask[]
  /** 展开详情面 */
  expanded: boolean
  /** 指针停留在岛内，队列倒计时在此期间挂起 */
  hovered: boolean
  /** 按消息语义静音，命中的入队请求被丢弃 */
  muted: Record<string, boolean>
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
  muted: {},
})

let seq = 0
let timer: number | null = null

function clearTimer() {
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
}

/** 当前展示项的就地移除 */
export function dropNotice(id: number) {
  const at = island.notices.findIndex((n) => n.id === id)
  if (at < 0) return
  island.notices.splice(at, 1)
  armTimer()
}

/** 按语义静音：清空该语义的在队项，并令其后续入队请求落空 */
export function muteTone(tone: IslandTone) {
  island.muted[tone] = true
  for (let i = island.notices.length - 1; i >= 0; i -= 1) {
    if (island.notices[i].tone === tone) island.notices.splice(i, 1)
  }
  armTimer()
}

/** 队首展示项的存活倒计时：队首更替即重排，悬停与展开期间挂起 */
export function armTimer() {
  clearTimer()
  const head = island.notices[0]
  if (!head || head.sticky || !head.duration) return
  if (island.expanded || island.hovered) return
  timer = window.setTimeout(() => {
    island.notices.shift()
    armTimer()
  }, head.duration)
}

/** 入队一条消息，返回其 id。被静音的语义返回 -1 */
export function pushNotice(notice: Omit<IslandNotice, 'id'>): number {
  if (island.muted[notice.tone]) return -1

  /* 进行中与结果共用同一个展示槽位：两者的在队项各自只留一条 */
  const stickyAt = island.notices.findIndex((n) => n.sticky)
  if (notice.sticky) {
    if (stickyAt >= 0) island.notices.splice(stickyAt, 1)
  } else if (stickyAt >= 0) {
    island.notices.splice(stickyAt, 1)
  }

  const item: IslandNotice = { ...notice, id: (seq += 1) }
  island.notices.unshift(item)
  if (island.notices.length > QUEUE_MAX) island.notices.length = QUEUE_MAX

  armTimer()
  return item.id
}

/** 更新在队项的文案（进行中的进度文本走这里） */
export function updateNotice(id: number, text: string) {
  const item = island.notices.find((n) => n.id === id)
  if (item) item.text = text
}

/** 展开面的开合；收起后恢复倒计时 */
export function setExpanded(on: boolean) {
  island.expanded = on
  armTimer()
}

/* ── 全局长任务注册层 ──
   store 发起的长任务（同步 / 检验 / 批量激活 / 内核下载）跨页存活，进度与
   完成情况走这里：任务在岛上是独立展示位，不占用消息槽位——结果消息入队
   不会顶掉还在跑的任务气泡，多个任务并存时展示最新一个、其余计数。 */

/** 注册（或就地刷新）一个进行中任务；同 key 重复注册视为续接 */
export function beginTask(key: string, text: string, opts?: { percent?: number | null; to?: string }) {
  const at = island.tasks.findIndex((task) => task.key === key)
  const task: IslandTask = { key, text, percent: opts?.percent ?? null, to: opts?.to ?? '' }
  if (at >= 0) island.tasks.splice(at, 1)
  island.tasks.unshift(task)
}

/** 更新在跑任务的进度行与定量进度 */
export function updateTask(key: string, text: string, percent?: number | null) {
  const task = island.tasks.find((t) => t.key === key)
  if (!task) return
  task.text = text
  if (percent !== undefined) task.percent = percent
}

/** 任务收场：撤下展示位（完成结果由发起方经 message 走消息队列） */
export function endTask(key: string) {
  const at = island.tasks.findIndex((task) => task.key === key)
  if (at >= 0) island.tasks.splice(at, 1)
}

/** 悬停态切换；进入即挂起倒计时，离开即恢复 */
export function setHovered(on: boolean) {
  island.hovered = on
  armTimer()
}
