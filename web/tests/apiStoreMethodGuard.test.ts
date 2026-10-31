import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readdirSync, readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import * as client from '../src/api/client.ts'

const storeDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'stores')

test('followsApi 三方法齐全（星标读取曾被误删致全站星标失效的回归守卫）', () => {
  assert.equal(typeof client.followsApi.list, 'function')
  assert.equal(typeof client.followsApi.add, 'function')
  assert.equal(typeof client.followsApi.remove, 'function')
})

test('stores 调用的每个 <ns>Api.<method> 都真实存在（含跨行拆写形态）', () => {
  const missing: string[] = []
  // 按方法名逐行 grep 扫不到「对象名与 .method 跨行拆写」的调用形态，必须整文扫描
  const callRe = /\b(\w+Api)\s*\.(\w+)\s*\(/g
  for (const file of readdirSync(storeDir).filter((f) => f.endsWith('.ts'))) {
    const text = readFileSync(join(storeDir, file), 'utf8')
    for (const m of text.matchAll(callRe)) {
      const ns = (client as Record<string, unknown>)[m[1]] as
        | Record<string, unknown>
        | undefined
      if (!ns || typeof ns[m[2]] !== 'function') missing.push(`${file}: ${m[1]}.${m[2]}`)
    }
  }
  assert.deepEqual(missing, [])
})
