import assert from 'node:assert/strict'
import { test } from 'node:test'

import { priceStatusOf } from '../src/lib/headerStatus.ts'
import en from '../src/locales/en/shell.ts'
import zh from '../src/locales/zh-CN/shell.ts'

const base = { running: false, done: 0, fail: 0, total: 0, hasHistory: false }

test('priceStatusOf：正在跑 → running（目标量未知时 total=0，标签退化为无分子分母）', () => {
  assert.equal(priceStatusOf({ ...base, running: true }).kind, 'running')
  assert.equal(
    priceStatusOf({ ...base, running: true, done: 24, total: 71 }).kind,
    'running',
  )
})

test('priceStatusOf：跑完且本轮零失败 → done（带本轮分子分母）', () => {
  const s = priceStatusOf({ ...base, done: 71, total: 71, hasHistory: true })
  assert.equal(s.kind, 'done')
  assert.equal(s.done, 71)
  assert.equal(s.total, 71)
  assert.equal(s.retry, 0)
})

test('priceStatusOf：跑完但有失败 → partial（不表达成「更新失败」，且给出稍后重试款数）', () => {
  const s = priceStatusOf({ ...base, done: 68, total: 71, fail: 3, hasHistory: true })
  assert.equal(s.kind, 'partial')
  assert.equal(s.retry, 3)
  assert.equal(s.done, 68)
})

test('priceStatusOf：本轮没跑完就停了 → partial（不得声称「价格已更新」）', () => {
  assert.equal(
    priceStatusOf({ ...base, done: 2, total: 4, lastStatus: 'stopped' }).kind,
    'partial',
  )
  assert.equal(
    priceStatusOf({ ...base, done: 2, total: 4, lastStatus: 'failed' }).kind,
    'partial',
  )
  // 同一个进度若无失败也无中断终态，才是完成
  assert.equal(
    priceStatusOf({ ...base, done: 4, total: 4, lastStatus: 'done' }).kind,
    'done',
  )
})

test('priceStatusOf：活动过后（hasHistory）不得回落成 waiting', () => {
  assert.equal(priceStatusOf({ ...base, hasHistory: true }).kind, 'idle')
  assert.equal(
    priceStatusOf({ ...base, hasHistory: true, lastStatus: 'done' }).kind,
    'idle',
  )
  assert.equal(priceStatusOf({ ...base, hasHistory: false }).kind, 'waiting')
})

test('priceStatusOf：没有活动——有历史 → idle，从没跑过 → waiting', () => {
  assert.equal(priceStatusOf({ ...base, hasHistory: true }).kind, 'idle')
  assert.equal(priceStatusOf(base).kind, 'waiting')
})

test('priceStatusOf：异常数值不产生负进度或 NaN 分母', () => {
  const s = priceStatusOf({
    running: false,
    done: Number.NaN,
    fail: -2,
    total: Number.POSITIVE_INFINITY,
    hasHistory: true,
  })
  assert.equal(s.done, 0)
  assert.equal(s.retry, 0)
  assert.equal(s.total, 0)
  assert.equal(s.kind, 'idle')
})

test('顶栏词条：主状态与补充信息不含内部术语（爬取 / 队列 / 速度 / worker / job / 代理）', () => {
  const keys = [
    'header.crawl',
    'header.crawlRunning',
    'header.crawlRunningBare',
    'header.crawlDone',
    'header.crawlIdle',
    'header.crawlPartial',
    'header.crawlTip',
    'header.crawlTipDone',
  ] as const
  const forbidden = [
    '爬取',
    '队列',
    '速度',
    '空闲',
    '节点',
    '代理',
    'crawl',
    'queue',
    'qsize',
    't/s',
    'worker',
    'job',
    'proxy',
    'idle',
  ]
  for (const dict of [zh, en] as Record<string, string>[]) {
    for (const key of keys) {
      const value = dict[key]
      assert.ok(typeof value === 'string' && value.length > 0, `${key} 缺词条`)
      for (const word of forbidden) {
        assert.ok(
          !value.includes(word),
          `${key} 命中内部术语「${word}」：${value}`,
        )
      }
    }
  }
})

test('顶栏词条：中英占位符逐条一致', () => {
  const keys = [
    'header.crawlRunning',
    'header.crawlDone',
    'header.crawlPartial',
    'header.crawlTip',
    'header.crawlTipDone',
  ] as const
  const holes = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort().join(',')
  for (const key of keys) {
    assert.equal(
      holes((zh as Record<string, string>)[key]!),
      holes((en as Record<string, string>)[key]!),
      `${key} 占位符不一致`,
    )
  }
})