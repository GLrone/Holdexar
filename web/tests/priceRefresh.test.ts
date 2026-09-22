import assert from 'node:assert/strict'
import { test } from 'node:test'

import { mergeItemsByAppid, nextPriceCycle } from '../src/lib/priceRefresh.ts'

test('nextPriceCycle：接受新的一轮', () => {
  assert.deepEqual(nextPriceCycle(null, { cycleId: 12, status: 'partial' }), {
    cycleId: 12,
    status: 'partial',
  })
  assert.deepEqual(nextPriceCycle({ cycleId: 11, status: 'completed' }, { cycleId: 12, status: 'failed' }), {
    cycleId: 12,
    status: 'failed',
  })
})

test('nextPriceCycle：同一轮重复到达不再推进（重复事件不变成重复请求）', () => {
  const current = { cycleId: 12, status: 'completed' }
  assert.equal(nextPriceCycle(current, { cycleId: 12, status: 'completed' }), null)
  assert.equal(nextPriceCycle(current, { cycleId: '12', status: 'completed' }), null)
})

test('nextPriceCycle：载荷不可用时不动状态', () => {
  assert.equal(nextPriceCycle(null, null), null)
  assert.equal(nextPriceCycle(null, '12'), null)
  assert.equal(nextPriceCycle(null, {}), null)
  assert.equal(nextPriceCycle(null, { cycleId: 'abc' }), null)
  assert.equal(nextPriceCycle(null, { cycleId: Number.NaN }), null)
})

test('nextPriceCycle：缺 status 时给空串，不落 undefined', () => {
  assert.deepEqual(nextPriceCycle(null, { cycleId: 3 }), { cycleId: 3, status: '' })
})

test('mergeItemsByAppid：数量与顺序不变，只换命中的条目', () => {
  const current = [
    { appid: 1, name: 'a', price: 100 },
    { appid: 2, name: 'b', price: 200 },
    { appid: 3, name: 'c', price: 300 },
  ]
  const fresh = new Map([
    [1, { appid: 1, name: 'a', price: 111 }],
    [3, { appid: 3, name: 'c', price: 333 }],
  ])
  const merged = mergeItemsByAppid(current, fresh)
  assert.deepEqual(
    merged.map((i) => i.appid),
    [1, 2, 3],
  )
  assert.deepEqual(
    merged.map((i) => i.price),
    [111, 200, 333],
  )
  assert.equal(merged.length, current.length)
})

test('mergeItemsByAppid：刷新里消失的对象保留原数据，不把列表打短', () => {
  const current = [
    { appid: 1, price: 100 },
    { appid: 2, price: 200 },
  ]
  const merged = mergeItemsByAppid(current, new Map([[1, { appid: 1, price: 150 }]]))
  assert.deepEqual(merged, [
    { appid: 1, price: 150 },
    { appid: 2, price: 200 },
  ])
})

test('mergeItemsByAppid：空列表与空刷新都不炸', () => {
  assert.deepEqual(mergeItemsByAppid([], new Map()), [])
  assert.deepEqual(mergeItemsByAppid([{ appid: 1, price: 1 }], new Map()), [{ appid: 1, price: 1 }])
})
