import assert from 'node:assert/strict'
import { test } from 'node:test'

import { notificationsApi, request } from '../src/api/client.ts'

test('local plugin uploads preserve archive bytes and use the authenticated API client', async () => {
  const original = globalThis.fetch
  const bytes = new Uint8Array([80, 75, 3, 4, 0, 255]).buffer
  let observed: RequestInit | undefined
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/v1/plugins/install-local')
    observed = init
    return new Response(JSON.stringify({ installed: true }))
  }
  try {
    assert.deepEqual(await request('POST', '/plugins/install-local', undefined, {
      rawBody: bytes,
      contentType: 'application/octet-stream',
    }), { installed: true })
    assert.equal(observed?.body, bytes)
    assert.equal(observed?.credentials, 'same-origin')
    assert.deepEqual(observed?.headers, { 'Content-Type': 'application/octet-stream' })
  } finally {
    globalThis.fetch = original
  }
})

test('plugin installation errors retain the server explanation', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Archive checksum mismatch' }), { status: 422 })
  try {
    await assert.rejects(request('POST', '/plugins/install-local', undefined, {
      rawBody: new ArrayBuffer(0),
      contentType: 'application/octet-stream',
    }), /Archive checksum mismatch/)
  } finally {
    globalThis.fetch = original
  }
})

test('notification preferences show an active plugin without SMTP configuration on the next read', async () => {
  const original = globalThis.fetch
  let calls = 0
  globalThis.fetch = async () => new Response(JSON.stringify({
    smtp: { configured: false },
    delivery: ++calls === 1
      ? { kind: 'smtp', configured: false }
      : { kind: 'plugin', configured: true, name: 'Test destination' },
  }))
  try {
    assert.equal((await notificationsApi.prefs()).delivery?.configured, false)
    const fresh = await notificationsApi.prefs()
    assert.equal(fresh.delivery?.kind, 'plugin')
    assert.equal(fresh.delivery?.configured, true)
    assert.equal(fresh.smtp.configured, false)
  } finally {
    globalThis.fetch = original
  }
})
