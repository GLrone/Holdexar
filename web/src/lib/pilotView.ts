/**
 * 领航台好评率视图换算：后端账本给 0–1 小数（0.872），展示层统一为
 * 百分整数（87%）与分级色（≥80 正常 / ≥60 警示 / 其余弱化，与游戏卡同阈值）。
 */

import type { MessageKey } from '@/locales'
import type {
  PilotAttachmentRef,
  PilotFacts,
  PilotFollowup,
  PilotGameFactsItem,
  PilotPhase,
  PilotStep,
} from '@/api/client'

/** 对话流单轮：ask/历史重放/快捷直达共同的上屏形态（Console 装配，TurnItem 渲染） */
export interface Turn {
  q: string
  tid?: string
  feedback?: 'accepted' | 'rejected' | null
  /** pending = 用户发言已上屏、回复未到的占位轮（回复完成后原位替换） */
  kind: 'candidates' | 'action' | 'price' | 'games' | 'answer' | 'guide' | 'reason' | 'pending' | 'compact'
    | 'proposal'
  /** compact 分隔线的触发来源（auto / manual），仅 kind=compact 时有值 */
  compactTrigger?: string
  /** 本轮被用户停止生成（保留已流出内容） */
  stopped?: boolean
  /** 账本里的轮次序号（编辑重问的定位依据）；未落账的轮为 undefined */
  turnIndex?: number
  text?: string
  reasonKey?: MessageKey
  diagnostic?: { id: string; code: string; message: string; status_code?: number | null }
  items?: PilotGameFactsItem[]
  cards?: PilotFacts[]
  cached?: boolean
  steps: PilotStep[]
  /** 按 agent 循环步分段的阶段记录（权威）；旧账本轮次无此项，过程链回落 think + steps */
  phases?: PilotPhase[]
  think?: string
  /** 思考用时（秒，服务端工时接入后随 done 下发；缺省不显示时长） */
  thinkSec?: number
  /** 整轮工时（秒，服务端墙钟含工具执行；缺省不显示时长） */
  elapsedSec?: number
  /** 过程链记忆快照：本轮上下文占用 + 归档边界 + 缓存命中 */
  mem?: { tokens: number | null; budget: number | null; archived: number | null; cached: boolean } | null
  /** 本轮随问携带的长文本附件（用户气泡下的附件芯片；点击展开内容） */
  attachments?: PilotAttachmentRef[]
  /** 本轮随问携带的编号引用（编号 + 正文）；气泡内按编号把标号渲染成胶囊 */
  quotes?: { n: number; text: string }[]
  /** 长回答是否展开（最新对话结果默认展开，发新对话时自动收敛；随轮持有，可手动切换） */
  expanded?: boolean
  /** 后端下发的本轮追问（结构化 key + args）；缺失时追问走本地卡片推导 */
  followups?: PilotFollowup[]
}

/** 附件尺寸摘要：≥10k 折 k 位（用户气泡附件芯片与输入区附件共用一口径） */
export function fmtChars(n: number): string {
  return n >= 10000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

export function positivePct(rate: number | null | undefined): number | null {
  if (typeof rate !== 'number' || rate <= 0) return null
  return Math.round(rate <= 1 ? rate * 100 : rate)
}

export function ratingClass(rate: number | null | undefined): string {
  const pct = positivePct(rate)
  if (pct === null) return 'low'
  if (pct >= 80) return ''
  if (pct >= 60) return 'medium'
  return 'low'
}

/** 封面 URL：账本只带 appid，按 Steam 公开素材路径拼（与游戏卡兜底同源） */
export function pilotCoverUrl(appid: number): string {
  return `https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/${appid}/header.jpg`
}

/** token 数展示：≥1000 缩写 K（一位小数），上下文占用与记忆条目共用 */
/** 引用展示标题：压平空白后截断（完整正文留给悬停面板；创建与回显两侧共用一口径） */
export function quoteTitleOf(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > 28 ? `${flat.slice(0, 28)}…` : flat
}

export function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n)
}

/** 会话列时间：今天显 HH:mm，今年显 MM-DD，更早显完整日期（账本 mtime ISO） */
export function sessionTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (v: number) => String(v).padStart(2, '0')
  const now = new Date()
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
  if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}`
  const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}-${md}`
}

export const ANSWER_CLAMP_CHARS = 700

/** 可评价的回答需要有对应会话轮次，并实际展示正文、卡片或步骤。 */
export function canRateTurn(turn: {
  tid?: string | null
  text?: string | null
  cards?: readonly unknown[] | null
  steps?: readonly unknown[] | null
}): boolean {
  return Boolean(turn.tid && (turn.text?.trim() || turn.cards?.length || turn.steps?.length))
}

/** 长回答判定：正文字符数超过阈值 */
export function isLongAnswerText(text: string | null | undefined): boolean {
  return (text?.length ?? 0) > ANSWER_CLAMP_CHARS
}

/** 失败/异常原因键集合（对应红色错误条件气泡与允许重试） */
export const ERROR_REASON_KEYS = new Set([
  'pilot.error',
  'pilot.reason.llm_failed',
  'pilot.reason.llm_failed_empty',
  'pilot.reason.busy',
  'pilot.reason.too_long',
  'pilot.reason.cap_reached',
  'pilot.reason.max_steps',
])

export function isErrorReason(reasonKey: string | null | undefined): boolean {
  if (!reasonKey) return false
  return ERROR_REASON_KEYS.has(reasonKey)
}

export function isEmptyLlmFailure(reason: string | null | undefined, source: string | null | undefined): boolean {
  return reason === 'llm_failed' && source === 'none'
}

