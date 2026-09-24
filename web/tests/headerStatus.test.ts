import assert from 'node:assert/strict'
import { test } from 'node:test'

import { priceStatusOf } from '../src/lib/headerStatus.ts'
import en from '../src/locales/en/shell.ts'
import zh from '../src/locales/zh-CN/shell.ts'

const base = { running: false, ok: 0, fail: 0, total: 0, hasHistory: false }

test('priceStatusOf：正在跑 → running（不产出任何数量）', () => {
  assert.equal(priceStatusOf({ ...base, running: true }), 'running')
  assert.equal(
    priceStatusOf({ ...base, running: true, ok: 24, fail: 3, total: 71 }),
    'running',
  )
})

test('priceStatusOf：全部成功 → done（success=5 failed=0 不得判成 partial）', () => {
  assert.equal(priceStatusOf({ ...base, ok: 5, fail: 0, total: 5, hasHistory: true }), 'done')
  assert.equal(priceStatusOf({ ...base, ok: 71, fail: 0, total: 71, hasHistory: true }), 'done')
})

test('priceStatusOf：全部失败 → partial（success=0 failed=5 绝不得判成 done）', () => {
  assert.equal(priceStatusOf({ ...base, ok: 0, fail: 5, total: 5, hasHistory: true }), 'partial')
})

test('priceStatusOf：部分失败 → partial（不表达成「更新失败」）', () => {
  assert.equal(
    priceStatusOf({ ...base, ok: 68, fail: 3, total: 71, hasHistory: true }),
    'partial',
  )
})

test('priceStatusOf：本轮没跑完就停了 → partial（不得声称「价格已更新」）', () => {
  assert.equal(
    priceStatusOf({ ...base, ok: 2, fail: 0, total: 4, lastStatus: 'stopped' }),
    'partial',
  )
  assert.equal(
    priceStatusOf({ ...base, ok: 2, fail: 0, total: 4, lastStatus: 'failed' }),
    'partial',
  )
  // 同一个进度若无失败也无中断终态，才是完成
  assert.equal(
    priceStatusOf({ ...base, ok: 4, fail: 0, total: 4, lastStatus: 'done' }),
    'done',
  )
})

test('priceStatusOf：有失败但目标量未知也 → partial（不得回落成已更新/等待更新）', () => {
  assert.equal(priceStatusOf({ ...base, ok: 0, fail: 3, total: 0, hasHistory: true }), 'partial')
})

test('priceStatusOf：活动过后（hasHistory）不得回落成 waiting', () => {
  assert.equal(priceStatusOf({ ...base, hasHistory: true }), 'idle')
  assert.equal(
    priceStatusOf({ ...base, hasHistory: true, lastStatus: 'done' }),
    'idle',
  )
  assert.equal(priceStatusOf({ ...base, hasHistory: false }), 'waiting')
})

test('priceStatusOf：没有活动——有历史 → idle，从没跑过 → waiting', () => {
  assert.equal(priceStatusOf({ ...base, hasHistory: true }), 'idle')
  assert.equal(priceStatusOf(base), 'waiting')
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

test('顶栏词条：主状态与补充信息不含内部术语与批次推导数量（爬取 / 队列 / 款 / games …）', () => {
  const keys = [
    'header.crawl',
    'header.crawlRunning',
    'header.crawlDone',
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
    // 内部计数口径是任务批次，不是游戏数——不得以「款 / games」出现在用户面
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
    for (const key of keys) {
      const value = dict[key]
      assert.ok(typeof value === 'string' && value.length > 0, `${key} 缺词条`)
      for (const word of forbidden) {
        assert.ok(
          !value.includes(word),
          `${key} 命中禁词「${word}」：${value}`,
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
