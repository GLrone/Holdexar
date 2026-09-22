import type { MessageKey } from '@/locales'
import type { PriceEventItem, PriceEventType } from '@/api/client'

import type { TextPart } from './priceDataView'

/**
 * 价格事件的展示规则（纯函数，不依赖 Vue）。
 *
 * 事件有没有发生由后端 `price_events` 判定，这里只做两件事：把内部枚举翻成用户
 * 看得懂的标签，把 previous / current 里的事实拼成「之前 → 现在」。
 *
 * **不产生事件**：前端不据 priceMatrix / hlFlag / ppFlag / price_status 自行推断，
 * 「本轮没抓到某区」也不是事件——只有后端确实写下对应行时才展示。
 */

/** 视觉分组：只做有限的配色区分，不做评分、不参与排序 */
export type EventTone = 'down' | 'low' | 'status' | 'free' | 'removed' | 'other'

const TONES: Record<PriceEventType, EventTone> = {
  PRICE_DROP: 'down',
  NEW_HISTORICAL_LOW: 'low',
  HISTORICAL_LOW_MATCH: 'low',
  REGION_LOCKED: 'status',
  REGION_UNLOCKED: 'status',
  PRICE_UNAVAILABLE: 'status',
  PRICE_RESTORED: 'status',
  FREE_PROMO: 'free',
  REMOVED: 'removed',
  PRICE_INCREASE: 'other',
  PERMANENT_PRICE_CHANGE: 'other',
}

/** 事件类型 → 用户标签。内部枚举不直接出现在界面上。 */
const LABELS: Record<PriceEventType, MessageKey> = {
  PRICE_DROP: 'priceEvent.type.PRICE_DROP',
  PRICE_INCREASE: 'priceEvent.type.PRICE_INCREASE',
  NEW_HISTORICAL_LOW: 'priceEvent.type.NEW_HISTORICAL_LOW',
  HISTORICAL_LOW_MATCH: 'priceEvent.type.HISTORICAL_LOW_MATCH',
  PERMANENT_PRICE_CHANGE: 'priceEvent.type.PERMANENT_PRICE_CHANGE',
  REGION_LOCKED: 'priceEvent.type.REGION_LOCKED',
  REGION_UNLOCKED: 'priceEvent.type.REGION_UNLOCKED',
  PRICE_UNAVAILABLE: 'priceEvent.type.PRICE_UNAVAILABLE',
  PRICE_RESTORED: 'priceEvent.type.PRICE_RESTORED',
  FREE_PROMO: 'priceEvent.type.FREE_PROMO',
  REMOVED: 'priceEvent.type.REMOVED',
}

/** 相对时长的词条（事件面用词：刚刚 / x 分钟前，与卡片的「刚刚更新」区分） */
export const EVENT_AGE_KEYS: AgeKeys = {
  none: 'priceEvent.ago.none',
  justNow: 'priceEvent.ago.justNow',
  minutes: 'priceEvent.ago.minutes',
  hours: 'priceEvent.ago.hours',
  days: 'priceEvent.ago.days',
}

/**
 * ISO 时刻 → 距现在的小时数；无效输入返回 null。
 *
 * 后端写的是本地时区的 naive ISO（同机部署下与浏览器同区）。小数秒截到 3 位：
 * `Date.parse` 对更长小数位不保证能解析。
 */
export function ageHoursOf(iso: string | null | undefined, nowMs = Date.now()): number | null {
  if (!iso) return null
  const parsed = Date.parse(iso.replace(/(\.\d{3})\d+/, '$1'))
  if (!Number.isFinite(parsed)) return null
  return Math.max(0, (nowMs - parsed) / 3_600_000)
}

/** `price_status` → 用户用词。状态跃迁里只说状态，不暴露 missing / blocked 这类内部词 */
const STATE_KEYS: Record<string, MessageKey> = {
  ok: 'priceEvent.state.ok',
  locked: 'priceEvent.state.locked',
  missing: 'priceEvent.state.missing',
  blocked: 'priceEvent.state.blocked',
}

/**
 * 事件取值出口：本币金额与价格状态词由调用方注入。
 * 前端注入 `formatMinor` + 状态词条，测试注入桩——判定逻辑之外的东西都不进本模块。
 */
export interface EventFormatters {
  /** 最小单位金额 + 区码 → 本地化金额文本 */
  price(amountMinor: number, region: string | null): string
  /** `price_status` → 用户用词 */
  state(status: string): string
}

/** 状态词出口的 i18n 实现（组件用；测试仍可注入桩） */
export function stateFormatter(translate: (key: MessageKey) => string): (status: string) => string {
  return (status) => translate(STATE_KEYS[status] ?? 'priceEvent.state.ok')
}

export interface EventView {
  /** `price_events.id`：事件的展示身份，去重与列表 key 都用它 */
  id: number
  type: PriceEventType
  tone: EventTone
  labelKey: MessageKey
  /** 大写区码；null = 游戏级事件（不属单一地区） */
  region: string | null
  /** 变化前 / 变化后；拿不到就是空串（不编造） */
  before: string
  after: string
  occurredAt: string | null
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asText(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

/** 事件类型 → 用户标签 key（摘要按类型计数时也用它） */
export function eventTypeLabelKey(type: PriceEventType): MessageKey {
  return (LABELS as Record<string, MessageKey>)[type] ?? 'priceEvent.type.OTHER'
}

/** 事件类型 → 视觉分组 */
export function eventToneOf(type: PriceEventType): EventTone {
  return (TONES as Record<string, EventTone>)[type] ?? 'other'
}

export function priceEventView(event: PriceEventItem, fmt: EventFormatters): EventView {
  const prev = event.previous ?? {}
  const cur = event.current ?? {}
  // 游戏级事件主体没有区码，但促销事件在 current 里记了探到的那个区
  const region = event.region ?? asText(cur.region)
  const price = (value: unknown): string => {
    const amount = asNumber(value)
    return amount === null ? '' : fmt.price(amount, region)
  }
  let before = ''
  let after = ''

  switch (event.eventType) {
    // 价格涨跌与史低都是「前一个有效价 → 本轮价」，只是前值的字段名不同
    case 'PRICE_DROP':
    case 'PRICE_INCREASE':
      before = price(prev.price)
      after = price(cur.price)
      break
    case 'NEW_HISTORICAL_LOW':
      before = price(prev.low)
      after = price(cur.price)
      break
    case 'HISTORICAL_LOW_MATCH':
      before = price(prev.price ?? prev.low)
      after = price(cur.price)
      break
    case 'PERMANENT_PRICE_CHANGE':
      before = price(prev.originalPrice)
      after = price(cur.originalPrice)
      break
    // 状态跃迁：事实就是「从哪个状态到哪个状态」
    case 'REGION_LOCKED':
    case 'REGION_UNLOCKED':
    case 'PRICE_UNAVAILABLE':
    case 'PRICE_RESTORED': {
      const from = asText(prev.status)
      const to = asText(cur.status)
      before = from ? fmt.state(from) : ''
      after = to ? fmt.state(to) : ''
      break
    }
    // 限时免费与下架是单事实事件：标签已说清，没有「之前 → 现在」
    default:
      break
  }

  // 表按 PriceEventType 穷举（新增类型时编译期报错）；运行时仍兜一层，
  // 后端加了新类型而前端没跟上时不该让整块渲染炸掉
  return {
    id: event.id,
    type: event.eventType,
    tone: eventToneOf(event.eventType),
    labelKey: eventTypeLabelKey(event.eventType),
    region,
    before,
    after,
    occurredAt: event.occurredAt,
  }
}

/** 事件 → 列表项。
 *
 * 相对时长由调用方注入（卡片与事件面共用同一套分档），`nowMs` 便于测试固定
 * 「距今多久」。`toAge` 通常就是 `agePart` 套上事件面词条。
 */
export function eventViews(
  events: readonly PriceEventItem[],
  fmt: EventFormatters,
  toAge: (ageHours: number | null) => TextPart,
  nowMs = Date.now(),
): (EventView & { age: TextPart })[] {
  return dedupeById(events).map((event) => ({
    ...priceEventView(event, fmt),
    age: toAge(ageHoursOf(event.occurredAt, nowMs)),
  }))
}

/**
 * 按 `price_events.id` 去重。
 * 列表刷新 / SSE / 重进页面 / 缓存失效都可能让同一条事件再次到达，展示身份只有 id。
 */
export function dedupeById(events: readonly PriceEventItem[]): PriceEventItem[] {
  const seen = new Set<number>()
  return events.filter((event) => {
    if (seen.has(event.id)) return false
    seen.add(event.id)
    return true
  })
}

export interface CycleEventSummary {
  /** 事件条数 */
  events: number
  /** 涉及的游戏数——与事件数不是一个量纲，界面上必须分开说 */
  games: number
  /** 按类型计数（只含出现过的类型），多的在前 */
  counts: { type: PriceEventType; count: number }[]
  /** 是否触到拉取上限：true 时上面的数字是下界 */
  truncated: boolean
}

/** 「本周期发生了什么」：对已经落库的事件做计数，不判定、不评分、不排序权重 */
export function summarizeCycle(
  events: readonly PriceEventItem[],
  limit: number,
): CycleEventSummary {
  const unique = dedupeById(events)
  const byType = new Map<PriceEventType, number>()
  const appids = new Set<number>()
  for (const event of unique) {
    byType.set(event.eventType, (byType.get(event.eventType) ?? 0) + 1)
    appids.add(event.appid)
  }
  const counts = [...byType.entries()]
    .map(([type, count]) => ({ type, count }))
    .sort((a, b) => b.count - a.count || a.type.localeCompare(b.type))
  return {
    events: unique.length,
    games: appids.size,
    counts,
    truncated: limit > 0 && unique.length >= limit,
  }
}
