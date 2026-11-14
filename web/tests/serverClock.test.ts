import assert from 'node:assert/strict'
import { test } from 'node:test'

import { serverOffsetMs } from '../src/lib/serverClock.ts'

test('serverOffsetMs：正常 ISO → 服务端钟减本机钟', () => {
  const local = 1_000
  assert.equal(serverOffsetMs('2026-11-04T12:00:00', local), Date.parse('2026-11-04T12:00:00') - local)
})

test('serverOffsetMs：缺失或不可解析按零偏移（没有可靠参照就不校正）', () => {
  assert.equal(serverOffsetMs(null, 1_000), 0)
  assert.equal(serverOffsetMs('', 1_000), 0)
  assert.equal(serverOffsetMs('not-a-date', 1_000), 0)
})
