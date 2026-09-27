import assert from 'node:assert/strict'
import { test } from 'node:test'

import { priceStatusOf } from '../src/lib/headerStatus.ts'
import en from '../src/locales/en/island.ts'
import zh from '../src/locales/zh-CN/island.ts'

const base = { running: false, ok: 0, fail: 0, total: 0, hasHistory: false }

test('priceStatusOf：正在跑 → running（不产出任何数量）', () => {
  assert.equal(priceStatusOf({ ...base, running: true }), 'running')
  assert.equal(priceStatusOf({ ...base, running: true, ok: 24, fail: 3, total: 71 }), 'running')
})

test('priceStatusOf：全部成功 → done（success=5 failed=0 不得判成 partial）', () => {
  assert.equal(priceStatusOf({ ...base, ok: 5, fail: 0, total: 5, hasHistory: true }), 'done')
  assert.equal(priceStatusOf({ ...base, ok: 71, fail: 0, total: 71, hasHistory: true }), 'done')
})

test('priceStatusOf：全部失败 → partial（success=0 failed=5 绝不得判成 done）', () => {
  assert.equal(priceStatusOf({ ...base, ok: 0, fail: 5, total: 5, hasHistory: true }), 'partial')
})

test('priceStatusOf：部分失败 → partial（不表达成「更新失败」）', () => {
  assert.equal(priceStatusOf({ ...base, ok: 68, fail: 3, total: 71, hasHistory: true }), 'partial')
})

test('priceStatusOf：本轮没跑完就停了 → partial（不得声称「价格已更新」）', () => {
  assert.equal(priceStatusOf({ ...base, ok: 2, fail: 0, total: 4, lastStatus: 'stopped' }), 'partial')
  assert.equal(priceStatusOf({ ...base, ok: 2, fail: 0, total: 4, lastStatus: 'failed' }), 'partial')
  // 同一个进度若无失败也无中断终态，才是完成
  assert.equal(priceStatusOf({ ...base, ok: 4, fail: 0, total: 4, lastStatus: 'done' }), 'done')
})

test('priceStatusOf：有失败但目标量未知也 → partial（不得回落成已更新/等待更新）', () => {
  assert.equal(priceStatusOf({ ...base, ok: 0, fail: 3, total: 0, hasHistory: true }), 'partial')
})

test('priceStatusOf：活动过后（hasHistory）不得回落成 waiting', () => {
  assert.equal(priceStatusOf({ ...base, hasHistory: true }), 'idle')
  assert.equal(priceStatusOf({ ...base, hasHistory: true, lastStatus: 'done' }), 'idle')
  assert.equal(priceStatusOf({ ...base, hasHistory: false }), 'waiting')
})

test('priceStatusOf：没有活动——有历史 → idle，从没跑过 → waiting', () => {
  assert.equal(priceStatusOf({ ...base, hasHistory: true }), 'idle')
  assert.equal(priceStatusOf(base), 'waiting')
})

test('priceStatusOf：最近一轮未收敛 → partial（零产出的 failed 也不得声称已更新）', () => {
  // 真实形态：一轮跑完 unitsOk 为 0，此前若干轮同样未收敛
  assert.equal(priceStatusOf({ ...base, lastStatus: 'failed', hasHistory: true }), 'partial')
  assert.equal(priceStatusOf({ ...base, lastStatus: 'partial', hasHistory: true }), 'partial')
  assert.equal(priceStatusOf({ ...base, lastStatus: 'cancelled', hasHistory: true }), 'partial')
})

test('priceStatusOf：轮次终态压过实时计数（本轮有成功也不改未收敛结论）', () => {
  assert.equal(
    priceStatusOf({ ...base, ok: 500, fail: 0, total: 500, lastStatus: 'failed', hasHistory: true }),
    'partial',
  )
})

test('priceStatusOf：最近一轮收敛 → done / idle', () => {
  assert.equal(priceStatusOf({ ...base, lastStatus: 'completed', hasHistory: true }), 'idle')
  assert.equal(
    priceStatusOf({
      ...base,
      ok: 714,
      fail: 0,
      total: 714,
      lastStatus: 'completed',
      hasHistory: true,
    }),
    'done',
  )
})

test('priceStatusOf：轮次尚未收束 → running（实时计数还没跟上也算）', () => {
  for (const lastStatus of ['planning', 'running', 'repairing', 'finalizing']) {
    assert.equal(priceStatusOf({ ...base, lastStatus }), 'running', lastStatus)
  }
})

test('priceStatusOf：异常数值不改变结论，只返回状态字符串', () => {
  assert.equal(
    priceStatusOf({
      running: false,
      ok: Number.NaN,
      fail: -2,
      total: Number.POSITIVE_INFINITY,
      hasHistory: true,
    }),
    'idle',
  )
})

const PRICE_KEYS = [
  'island.price.waiting',
  'island.price.running',
  'island.price.done',
  'island.price.partial',
  'island.price.tip',
  'island.price.tipDone',
  'island.ago.justNow',
  'island.ago.minutes',
  'island.ago.hours',
  'island.ago.days',
] as const

test('岛内价格词条：不含内部术语与批次推导数量（爬取 / 队列 / 款 / games …）', () => {
  const forbidden = [
    '爬取',
    '队列',
    '速度',
    '空闲',
    '节点',
    '代理',
    // 内部计数口径是刷新轮次与任务批次，不是游戏数——不得以「款 / games」出现在用户面
    '款',
    'crawl',
    'queue',
    'qsize',
    't/s',
    'worker',
    'job',
    'proxy',
    'idle',
    'games',
  ]
  for (const dict of [zh, en] as Record<string, string>[]) {
    for (const key of PRICE_KEYS) {
      const value = dict[key]
      assert.ok(typeof value === 'string' && value.length > 0, `${key} 缺词条`)
      for (const word of forbidden) {
        assert.ok(!value.includes(word), `${key} 命中禁词「${word}」：${value}`)
      }
    }
  }
})

test('岛内价格词条：中英占位符逐条一致', () => {
  const keys = ['island.price.updatedAt', 'island.ago.minutes', 'island.ago.hours', 'island.ago.days'] as const
  const holes = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort().join(',')
  for (const key of keys) {
    assert.ok(holes((zh as Record<string, string>)[key]!), `${key} 值里没有占位符`)
    assert.equal(
      holes((zh as Record<string, string>)[key]!),
      holes((en as Record<string, string>)[key]!),
      `${key} 占位符不一致`,
    )
  }
})
