import assert from 'node:assert/strict'
import { createServer as createBackend } from 'node:http'
import type { AddressInfo } from 'node:net'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { createServer as createVite } from 'vite'

test('开发代理保留页面 Host 和原始 Origin，包括跨源请求', async t => {
  const backend = createBackend((request, response) => {
    response.setHeader('Content-Type', 'application/json')
    response.end(JSON.stringify({ host: request.headers.host, origin: request.headers.origin }))
  })
  await new Promise<void>(resolve => backend.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise<void>((resolve, reject) => {
    backend.close(error => error ? reject(error) : resolve())
    backend.closeAllConnections()
  }))

  const previousTarget = process.env.VITE_PROXY_TARGET
  process.env.VITE_PROXY_TARGET = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`
  let vite: Awaited<ReturnType<typeof createVite>>
  try {
    vite = await createVite({
      configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
      root: fileURLToPath(new URL('..', import.meta.url)),
      server: { host: '127.0.0.1', port: 0, hmr: false },
      optimizeDeps: { noDiscovery: true, include: [] },
      logLevel: 'silent',
    })
  } finally {
    if (previousTarget === undefined) delete process.env.VITE_PROXY_TARGET
    else process.env.VITE_PROXY_TARGET = previousTarget
  }
  t.after(() => vite.close())
  await vite.listen()
  const authority = `127.0.0.1:${(vite.httpServer!.address() as AddressInfo).port}`
  for (const origin of [`http://${authority}`, 'http://127.0.0.1:9000', 'https://evil.example']) {
    const response = await fetch(`http://${authority}/api/v1/health`, {
      method: 'POST', headers: { Origin: origin }, body: '{}',
    })
    assert.equal(response.status, 200)
    assert.deepEqual(await response.json(), { host: authority, origin })
  }
})
