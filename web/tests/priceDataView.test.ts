import assert from 'node:assert/strict'
import { test } from 'node:test'

import { agePart, coverageTipParts, coverageView, priceDataView } from '../src/lib/priceDataView.ts'
import type { PriceCoverage } from '../src/api/client.ts'

function cov(over: Partial<PriceCoverage> = {}): PriceCoverage {
  return {
    cycleId: 7,
    cycleStatus: 'partial',
    expectedUnits: 36,
    ok: 32,
    locked: 3,
    missing: 0,
    blocked: 0,
    unobserved: 1,
    coverage: 0.8889,
    coverageConfirmed: 0.9722,
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
  // 已确认（ok+locked）达到期望 → null（卡片不出覆盖 chip）
  assert.equal(coverageView(cov({ ok: 36, locked: 0, unobserved: 0 })), null)
  // 锁区计入确认：4 ok + 2 locked = 36/36 → 不显示
  assert.equal(coverageView(cov({ ok: 34, locked: 2, unobserved: 0 })), null)
  const partial = coverageView(cov())
  assert.equal(partial?.key, 'gameCard.priceData.coveragePartial')
  assert.equal(partial?.partial, true)
  assert.deepEqual(partial?.params, { ok: 35, expected: 36 })
})

test('coverageView：没有 Cycle 归属就不给覆盖（不冒充 100%）', () => {
  assert.equal(coverageView(null), null)
  assert.equal(coverageView(undefined), null)
})

test('coverageTipParts：有区级明细就逐区点名，不合并成「失败」', () => {
  const name = (code: string) => (code === 'RU' ? '俄罗斯' : code)
  assert.deepEqual(
    coverageTipParts(
      cov({ regions: { RU: 'locked', PK: 'missing', IN: 'unobserved' } }),
      name,
    ),
    [
      { key: 'gameCard.priceData.tipRegion.locked', params: { region: '俄罗斯' } },
      { key: 'gameCard.priceData.tipRegion.failed', params: { region: 'PK' } },
      { key: 'gameCard.priceData.tipRegion.unobserved', params: { region: 'IN' } },
    ],
  )
  // blocked 与 missing 同属「没拿到」，同一措辞
  const parts = coverageTipParts(cov({ regions: { UA: 'blocked' } }), name)
  assert.deepEqual(parts, [
    { key: 'gameCard.priceData.tipRegion.failed', params: { region: 'UA' } },
  ])
})

test('coverageTipParts：旧快照无区明细退回计数措辞（不虚构区名）', () => {
  assert.deepEqual(coverageTipParts(cov({ locked: 3, missing: 0, blocked: 0, unobserved: 1 })), [
    { key: 'gameCard.priceData.tip.locked', params: { n: 3 } },
    { key: 'gameCard.priceData.tip.unobserved', params: { n: 1 } },
  ])
  // missing + blocked 同属「没抓到」，合并计数；与锁区、未观察分开
  assert.deepEqual(coverageTipParts(cov({ ok: 30, locked: 2, missing: 3, blocked: 1, unobserved: 0 })), [
    { key: 'gameCard.priceData.tip.locked', params: { n: 2 } },
    { key: 'gameCard.priceData.tip.failed', params: { n: 4 } },
  ])
  // 有明细但没给解析器：同样退回计数
  assert.deepEqual(
    coverageTipParts(cov({ regions: { RU: 'locked' } })),
    [
      { key: 'gameCard.priceData.tip.locked', params: { n: 3 } },
      { key: 'gameCard.priceData.tip.unobserved', params: { n: 1 } },
    ],
  )
})

test('coverageTipParts：完全刷满时没有可说的，不产空话', () => {
  assert.deepEqual(coverageTipParts(cov({ ok: 36, locked: 0, unobserved: 0 })), [])
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
      coverage: cov({ regions: { RU: 'locked' } }),
    },
    (code) => (code === 'RU' ? '俄罗斯' : code),
  )
  assert.deepEqual(view.tips, [
    { key: 'gameCard.priceData.tipRegion.locked', params: { region: '俄罗斯' } },
  ])
})
