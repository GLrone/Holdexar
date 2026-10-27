import assert from 'node:assert/strict'
import { test } from 'node:test'

import { bundlesApi } from '../src/api/client.ts'

test('bundle pages send global filters and retain server totals without a second response cache', async () => {
  const requests: URL[] = []
  const original = globalThis.fetch
  globalThis.fetch = async (url) => {
    requests.push(new URL(String(url), 'http://localhost'))
    return new Response(JSON.stringify({ bundles: [], total: 517, completableCount: 123, offset: 120, limit: 120 }))
  }
  try {
    const query = { offset: 120, limit: 120, region: 'JP', region_mode: 'locked' as const,
      hide_owned: true, hide_family: true, gift_only: true, query: 'sample', diff_min: 25, diff_type: 'percent' as const }
    const first = await bundlesApi.list(query)
    await bundlesApi.list(query)
    assert.equal(first.total, 517)
    assert.equal(first.completableCount, 123)
    assert.equal(requests.length, 2)
    assert.equal(requests[0].pathname, '/api/v1/bundles')
    for (const [key, value] of Object.entries(query)) assert.equal(requests[0].searchParams.get(key), String(value))
  } finally {
    globalThis.fetch = original
  }
})
