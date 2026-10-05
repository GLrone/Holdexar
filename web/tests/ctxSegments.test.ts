import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  buildCtxSegments,
  shouldShowCacheHitRate,
  CACHE_HIT_RATE_DISPLAY_THRESHOLD,
} from '../src/lib/ctxSegments.ts'

test('份额降序；同份额按固定来源序', () => {
  const segs = buildCtxSegments([
    { source: 'history', chars: 300 },
    { source: 'tools', chars: 300 },
    { source: 'system', chars: 400 },
  ])
  assert.deepEqual(
    segs.map((s) => s.source),
    ['system', 'tools', 'history'],
  )
  assert.equal(segs[0].share, 0.4)
})

test('非正数与非法值剔除；全空返回空数组', () => {
  assert.deepEqual(buildCtxSegments([{ source: 'system', chars: 0 }, { source: 'tools', chars: -5 }]), [])
  assert.deepEqual(buildCtxSegments([{ source: 'system', chars: Number.NaN }]), [])
  assert.deepEqual(buildCtxSegments(null), [])
  assert.deepEqual(buildCtxSegments(undefined), [])
})

test('未知来源排到同份额末尾且不破坏排序', () => {
  const segs = buildCtxSegments([
    { source: 'mystery', chars: 100 },
    { source: 'current', chars: 100 },
  ])
  assert.deepEqual(
    segs.map((s) => s.source),
    ['current', 'mystery'],
  )
})

test('命中率阈值：0.78 以下生产不展示，DEV 绕过', () => {
  assert.equal(shouldShowCacheHitRate(0.5), false)
  assert.equal(shouldShowCacheHitRate(0.5, true), true)
  assert.equal(shouldShowCacheHitRate(CACHE_HIT_RATE_DISPLAY_THRESHOLD), true)
  assert.equal(shouldShowCacheHitRate(0.9), true)
  assert.equal(shouldShowCacheHitRate(null), false)
  assert.equal(shouldShowCacheHitRate(Number.NaN), false)
})
