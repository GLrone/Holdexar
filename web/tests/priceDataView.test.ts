import assert from 'node:assert/strict'
import { test } from 'node:test'

import { agePart, coverageTipParts, coverageView, priceDataView } from '../src/lib/priceDataView.ts'
import type { PriceCoverage } from '../src/api/client.ts'

function cov(over: Partial<PriceCoverage> = {}): PriceCoverage {
  return {
    expectedUnits: 36,
    success: 35,
    failed: 0,
    notAttempted: 1,
    coverage: 0.9722,
    regions: { IN: { outcome: 'notAttempted', answer: null, lastSuccessAt: null } },
    ...over,
  }
}

test('agePart：分档边界', () => {
  assert.deepEqual(agePart(null), { key: 'gameCard.priceData.none' })
  assert.deepEqual(agePart(undefined), { key: 'gameCard.priceData.none' })
  assert.deepEqual(agePart(0.001), { key: 'gameCard.priceData.justNow' })
  assert.deepEqual(agePart(0.5), { key: 'gameCard.priceData.minutesAgo', params: { n: 30 } })
  assert.deepEqual(agePart(2.9), { key: 'gameCard.priceData.hoursAgo', params: { n: 2 } })
  assert.deepEqual(agePart(50), { key: 'gameCard.priceData.daysAgo', params: { n: 2 } })
})

test('coverageView：刷满不显示覆盖标签，未刷满才点明未完整', () => {
  // 成功观察（含 locked / 无选项）达到期望 → null（卡片不出覆盖 chip）
  assert.equal(coverageView(cov({ success: 36, failed: 0, notAttempted: 0, regions: {} })), null)
  const partial = coverageView(cov())
  assert.equal(partial?.key, 'gameCard.priceData.coveragePartial')
  assert.equal(partial?.partial, true)
  assert.deepEqual(partial?.params, { ok: 35, expected: 36 })
})

test('coverageView：没有覆盖数据就不给标签（不冒充 100%）', () => {
  assert.equal(coverageView(null), null)
  assert.equal(coverageView(undefined), null)
})

test('coverageTipParts：失败逐区点名，带上次成功时刻；未尝试单独措辞', () => {
  const name = (code: string) => (code === 'RU' ? '俄罗斯' : code)
  assert.deepEqual(
    coverageTipParts(
      cov({
        success: 34,
        failed: 1,
        notAttempted: 1,
        regions: {
          RU: { outcome: 'failed', answer: null, lastSuccessAt: '2026-10-02T12:05:00' },
          PK: { outcome: 'failed', answer: null, lastSuccessAt: null },
          IN: { outcome: 'notAttempted', answer: null, lastSuccessAt: null },
        },
      }),
      name,
    ),
    [
      {
        key: 'gameCard.priceData.tipRegion.failedStale',
        params: { region: '俄罗斯', time: '10-02 12:05' },
      },
      { key: 'gameCard.priceData.tipRegion.failed', params: { region: 'PK' } },
      { key: 'gameCard.priceData.tipRegion.notAttempted', params: { region: 'IN' } },
    ],
  )
})

test('coverageTipParts：完全刷满时没有可说的，不产空话', () => {
  assert.deepEqual(coverageTipParts(cov({ success: 36, failed: 0, notAttempted: 0, regions: {} })), [])
  assert.deepEqual(coverageTipParts(null), [])
})

test('priceDataView：组合观察时间 / 覆盖 / 新鲜度，缺失时全部降级', () => {
  const view = priceDataView({
    observedAt: '2026-09-21T18:00:00',
    ageHours: 3,
    freshness: 'fresh',
    coverage: cov(),
  })
  assert.deepEqual(view.age, { key: 'gameCard.priceData.hoursAgo', params: { n: 3 } })
  assert.equal(view.coverage?.partial, true)
  assert.equal(view.freshness, 'fresh')

  const empty = priceDataView(null)
  assert.deepEqual(empty.age, { key: 'gameCard.priceData.none' })
  assert.equal(empty.coverage, null)
  assert.deepEqual(empty.tips, [])
  assert.equal(empty.freshness, null)
})

test('priceDataView：解析器透传给区级点名', () => {
  const view = priceDataView(
    {
      observedAt: '2026-09-21T18:00:00',
      ageHours: 3,
      freshness: 'fresh',
      coverage: cov({
        regions: { RU: { outcome: 'failed', answer: null, lastSuccessAt: null } },
      }),
    },
    (code) => (code === 'RU' ? '俄罗斯' : code),
  )
  assert.deepEqual(view.tips, [
    { key: 'gameCard.priceData.tipRegion.failed', params: { region: '俄罗斯' } },
  ])
})
