import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  dedupeById,
  EVENT_AGE_KEYS,
  eventToneOf,
  eventTypeLabelKey,
  eventViews,
  priceEventView,
  summarizeCycle,
  type EventFormatters,
} from '../src/lib/priceEvents.ts'
import { agePart } from '../src/lib/priceDataView.ts'
import type { PriceEventItem, PriceEventType } from '../src/api/client.ts'

/** 桩：金额直接换成「{n}{币}」，状态换成「state:{s}」——断言只看映射，不看格式化 */
const fmt: EventFormatters = {
  price: (amountMinor, region) => `${amountMinor}${region ?? ''}`,
  state: (status) => `state:${status}`,
}

const toAge = (hours: number | null) => agePart(hours, EVENT_AGE_KEYS)

function event(over: Partial<PriceEventItem> & { eventType: PriceEventType }): PriceEventItem {
  return {
    id: 1,
    cycleId: 9,
    appid: 620,
    region: 'RU',
    previous: null,
    current: null,
    occurredAt: '2026-09-21T18:00:00.123456',
    ...over,
  }
}

test('标签来自映射表，内部枚举不出现在界面上', () => {
  assert.equal(eventTypeLabelKey('PRICE_DROP'), 'priceEvent.type.PRICE_DROP')
  assert.equal(eventTypeLabelKey('NEW_HISTORICAL_LOW'), 'priceEvent.type.NEW_HISTORICAL_LOW')
  assert.equal(eventTypeLabelKey('REGION_LOCKED'), 'priceEvent.type.REGION_LOCKED')
  assert.equal(eventTypeLabelKey('PRICE_RESTORED'), 'priceEvent.type.PRICE_RESTORED')
  assert.equal(eventTypeLabelKey('FREE_PROMO'), 'priceEvent.type.FREE_PROMO')
  assert.equal(eventTypeLabelKey('REMOVED'), 'priceEvent.type.REMOVED')
  // 后端先于前端上了新类型：不炸，回落「价格变化」
  assert.equal(
    eventTypeLabelKey('BRAND_NEW' as PriceEventType),
    'priceEvent.type.OTHER',
  )
})

test('视觉分组只做有限区分', () => {
  assert.equal(eventToneOf('PRICE_DROP'), 'down')
  assert.equal(eventToneOf('NEW_HISTORICAL_LOW'), 'low')
  assert.equal(eventToneOf('HISTORICAL_LOW_MATCH'), 'low')
  assert.equal(eventToneOf('REGION_LOCKED'), 'status')
  assert.equal(eventToneOf('PRICE_RESTORED'), 'status')
  assert.equal(eventToneOf('FREE_PROMO'), 'free')
  assert.equal(eventToneOf('REMOVED'), 'removed')
})

test('价格涨跌：之前 → 现在，金额按事件所在区格式化', () => {
  const view = priceEventView(
    event({
      eventType: 'PRICE_DROP',
      previous: { price: 19900, cnyFen: 1500 },
      current: { price: 9900, cnyFen: 800 },
    }),
    fmt,
  )
  assert.equal(view.before, '19900RU')
  assert.equal(view.after, '9900RU')
  assert.equal(view.region, 'RU')
})

test('历史新低：前值是窗口前的最低价', () => {
  const view = priceEventView(
    event({
      eventType: 'NEW_HISTORICAL_LOW',
      region: 'CN',
      previous: { low: 3900 },
      current: { price: 2900 },
    }),
    fmt,
  )
  assert.equal(view.before, '3900CN')
  assert.equal(view.after, '2900CN')
})

test('追平史低：优先用上一价，缺失时回落最低价', () => {
  const withPrev = priceEventView(
    event({
      eventType: 'HISTORICAL_LOW_MATCH',
      region: 'CN',
      previous: { low: 2900, price: 5900 },
      current: { price: 2900 },
    }),
    fmt,
  )
  assert.equal(withPrev.before, '5900CN')
  assert.equal(withPrev.after, '2900CN')

  const withoutPrev = priceEventView(
    event({
      eventType: 'HISTORICAL_LOW_MATCH',
      region: 'CN',
      previous: { low: 2900 },
      current: { price: 2900 },
    }),
    fmt,
  )
  assert.equal(withoutPrev.before, '2900CN')
})

test('原价调整：比较的是 originalPrice', () => {
  const view = priceEventView(
    event({
      eventType: 'PERMANENT_PRICE_CHANGE',
      region: 'CN',
      previous: { originalPrice: 19900 },
      current: { originalPrice: 9900 },
    }),
    fmt,
  )
  assert.equal(view.before, '19900CN')
  assert.equal(view.after, '9900CN')
})

test('状态跃迁：说「从哪个状态到哪个状态」，不暴露内部词', () => {
  const locked = priceEventView(
    event({
      eventType: 'REGION_LOCKED',
      previous: { status: 'ok' },
      current: { status: 'locked' },
    }),
    fmt,
  )
  assert.equal(locked.before, 'state:ok')
  assert.equal(locked.after, 'state:locked')

  const restored = priceEventView(
    event({
      eventType: 'PRICE_RESTORED',
      previous: { status: 'blocked' },
      current: { status: 'ok', cnyFen: 9900 },
    }),
    fmt,
  )
  assert.equal(restored.before, 'state:blocked')
  assert.equal(restored.after, 'state:ok')
})

test('限时免费：游戏级事件取 current.region 当展示区，无前后值', () => {
  const view = priceEventView(
    event({
      eventType: 'FREE_PROMO',
      region: null,
      previous: { freeKind: null },
      current: { freeKind: 'promo', region: 'cn' },
    }),
    fmt,
  )
  assert.equal(view.region, 'cn')
  assert.equal(view.before, '')
  assert.equal(view.after, '')
})

test('下架：游戏级事件，没有地区也没有前后值', () => {
  const view = priceEventView(
    event({
      eventType: 'REMOVED',
      region: null,
      previous: { removedAt: null },
      current: { removedAt: '2026-09-21T18:00:00' },
    }),
    fmt,
  )
  assert.equal(view.region, null)
  assert.equal(view.before, '')
  assert.equal(view.after, '')
})

test('相对时间用事件自身的 occurred_at，与实体更新时间无关', () => {
  const now = Date.parse('2026-09-21T20:00:00')
  const views = eventViews(
    [
      event({ id: 1, eventType: 'PRICE_DROP', occurredAt: '2026-09-21T19:30:00' }),
      event({ id: 2, eventType: 'REMOVED', occurredAt: '2026-09-21T08:00:00' }),
      event({ id: 3, eventType: 'REMOVED', occurredAt: null }),
      event({ id: 4, eventType: 'REMOVED', occurredAt: '不是时间' }),
      // 后端带 6 位小数秒：截到 3 位后可解析，30 分钟差 123ms → floor 29
      event({ id: 5, eventType: 'PRICE_DROP', occurredAt: '2026-09-21T19:30:00.123456' }),
    ],
    fmt,
    toAge,
    now,
  )
  assert.deepEqual(views[0].age, { key: 'priceEvent.ago.minutes', params: { n: 30 } })
  // 12 小时前
  assert.deepEqual(views[1].age, { key: 'priceEvent.ago.hours', params: { n: 12 } })
  assert.deepEqual(views[2].age, { key: 'priceEvent.ago.none' })
  assert.deepEqual(views[3].age, { key: 'priceEvent.ago.none' })
  assert.deepEqual(views[4].age, { key: 'priceEvent.ago.minutes', params: { n: 29 } })
})

test('去重：同一 price_events.id 只展示一次（列表刷新 / SSE / 重进页面）', () => {
  const rows = [
    event({ id: 7, eventType: 'PRICE_DROP' }),
    event({ id: 8, eventType: 'NEW_HISTORICAL_LOW' }),
    event({ id: 7, eventType: 'PRICE_DROP' }),
  ]
  assert.deepEqual(
    dedupeById(rows).map((r) => r.id),
    [7, 8],
  )
  // eventViews 内部也先去重
  assert.equal(eventViews(rows, fmt, toAge).length, 2)
})

test('空事件列表：不产生任何展示项（不编造「价格不可用」）', () => {
  const views = eventViews([], fmt, toAge)
  assert.deepEqual(views, [])
})

test('本轮摘要：事件数与游戏数分开算', () => {
  const rows = [
    event({ id: 1, appid: 1, eventType: 'PRICE_DROP' }),
    event({ id: 2, appid: 1, eventType: 'PRICE_DROP' }),
    event({ id: 3, appid: 2, eventType: 'PRICE_DROP' }),
    event({ id: 4, appid: 2, eventType: 'NEW_HISTORICAL_LOW' }),
    event({ id: 5, appid: 2, eventType: 'PRICE_RESTORED' }),
  ]
  const summary = summarizeCycle(rows, 200)
  assert.equal(summary.events, 5)
  assert.equal(summary.games, 2)
  assert.deepEqual(
    summary.counts.map((c) => [c.type, c.count]),
    [
      ['PRICE_DROP', 3],
      ['NEW_HISTORICAL_LOW', 1],
      ['PRICE_RESTORED', 1],
    ],
  )
  assert.equal(summary.truncated, false)
})

test('本轮摘要：计数相同按类型名排序，触到上限时数字是下界', () => {
  const rows = [
    event({ id: 1, appid: 1, eventType: 'PRICE_DROP' }),
    event({ id: 2, appid: 2, eventType: 'NEW_HISTORICAL_LOW' }),
  ]
  const summary = summarizeCycle(rows, 2)
  assert.deepEqual(
    summary.counts.map((c) => c.type),
    ['NEW_HISTORICAL_LOW', 'PRICE_DROP'],
  )
  assert.equal(summary.truncated, true)
})

test('本轮摘要：去重之后再计数', () => {
  const rows = [
    event({ id: 1, appid: 1, eventType: 'PRICE_DROP' }),
    event({ id: 1, appid: 1, eventType: 'PRICE_DROP' }),
  ]
  const summary = summarizeCycle(rows, 200)
  assert.equal(summary.events, 1)
  assert.equal(summary.games, 1)
})
