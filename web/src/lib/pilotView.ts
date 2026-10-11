/**
 * 领航台好评率视图换算：后端账本给 0–1 小数（0.872），展示层统一为
 * 百分整数（87%）与分级色（≥80 正常 / ≥60 警示 / 其余弱化，与游戏卡同阈值）。
 */

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

/** 长回答判定：正文字符数超过阈值 */
export function isLongAnswerText(text: string | null | undefined): boolean {
  return (text?.length ?? 0) > ANSWER_CLAMP_CHARS
}

/** 失败/异常原因键集合（对应红色错误条件气泡与允许重试） */
export const ERROR_REASON_KEYS = new Set([
  'pilot.error',
  'pilot.reason.llm_failed',
  'pilot.reason.busy',
  'pilot.reason.too_long',
  'pilot.reason.cap_reached',
  'pilot.reason.max_steps',
])

export function isErrorReason(reasonKey: string | null | undefined): boolean {
  if (!reasonKey) return false
  return ERROR_REASON_KEYS.has(reasonKey)
}

