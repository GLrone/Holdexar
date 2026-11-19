import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  QUEUE_SEGMENT_SPECS,
  resolveQueueChain,
  resolveQueueSegments,
} from '../src/lib/queueFlow.ts'

test('queue segments：组成里出现的段亮、缺失的段灰，段定义顺序固定', () => {
  const segments = resolveQueueSegments(['missing', 'pool', 'catalog', 'specials'])
  assert.deepEqual(
    segments.map((s) => [s.key, s.on]),
    [
      ['missing', true],
      ['pool', true],
      ['discounts', false],
      ['catalog', true],
      ['specials', true],
    ],
  )
})

test('queue chain：总队列链按返回顺序原样投影（真实组队顺序）', () => {
  const chain = resolveQueueChain(['pool', 'missing', 'discounts'])
  assert.deepEqual(chain.map((s) => s.key), ['pool', 'missing', 'discounts'])
  assert.ok(chain.every((s) => s.on))
})

test('queue chain：未知段名忽略（后端新增段时前端不炸）', () => {
  const chain = resolveQueueChain(['missing', 'future_scope'])
  assert.deepEqual(chain.map((s) => s.key), ['missing'])
})

test('queue segments：特惠榜尾段无独立开关（跟随目录层），其余四段带设置字段', () => {
  const byKey = new Map(QUEUE_SEGMENT_SPECS.map((s) => [s.key, s]))
  assert.equal(byKey.get('specials')?.setting, null)
  assert.equal(byKey.get('missing')?.setting, 'queue_missing')
  assert.equal(byKey.get('pool')?.setting, 'queue_pool')
  assert.equal(byKey.get('discounts')?.setting, 'queue_discounts')
  assert.equal(byKey.get('catalog')?.setting, 'catalog_refresh')
})
