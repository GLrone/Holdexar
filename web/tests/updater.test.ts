import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import { setImmediate } from 'node:timers/promises'
import { test, type TestContext } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'

const defaults = {
  updatePending: async () => ({ pending: false, installing: false }),
  updateProgress: async () => ({ running: false, ok: false, code: null, error: null }),
  updateCancel: async () => ({}),
  updateAck: async () => ({ acknowledged: true }),
  info: async () => ({ version: 'new' }),
}
const api = { ...defaults }
globalThis.__updaterApi = api
const source = stripTypeScriptTypes(readFileSync(new URL('../src/stores/updater.ts', import.meta.url), 'utf8'))
  .replace("from 'vue'", `from '${import.meta.resolve('vue')}'`)
  .replace("from 'pinia'", `from '${import.meta.resolve('pinia')}'`)
  .replace(/import .* from '@\/api\/client'/, 'const systemApi = globalThis.__updaterApi')
  .replace(/import .* from '@\/components\/ui\/activity'/, 'const activityManager = { register() {}, remove() {} }')
  .replace(/import .* from '@\/locales'/, 'const useI18n = () => ({ t: key => key })')
  .replace(/import .* from '@\/stores\/settings'/, 'const useSettingsStore = () => ({ updateSkipped: "" })')
const { useUpdaterStore } = await import(`data:text/javascript,${encodeURIComponent(source)}`)

function setup(t: TestContext) {
  Object.assign(api, defaults)
  setActivePinia(createPinia())
  const timers = new Map<number, () => void>()
  let id = 0
  t.mock.method(globalThis, 'setInterval', (callback: () => void) => {
    timers.set(++id, callback)
    return id
  })
  t.mock.method(globalThis, 'clearInterval', (timer: number) => timers.delete(timer))
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => 'old', setItem() {} } })
  return {
    store: useUpdaterStore(), timers,
    async tick() {
      for (const callback of [...timers.values()]) callback()
      await setImmediate()
    },
  }
}

test('首次界面从验收中自动跟随到提交成功，复用单个轮询并收尾', async (t) => {
  const { store, timers, tick } = setup(t)
  let pending = { pending: false, installing: true, justUpdated: null }
  t.mock.method(api, 'updatePending', async () => pending)
  const progress = t.mock.method(api, 'updateProgress', async () => ({ running: false }))
  await store.sync()
  assert.equal(store.pendingTag, '')
  assert.equal(store.justUpdated, null)
  assert.equal(timers.size, 1)
  await tick()
  assert.equal(timers.size, 1)
  pending = { pending: false, installing: false, justUpdated: { previousVersion: 'old', currentVersion: 'new' } }
  await tick()
  assert.equal(store.justUpdated?.currentVersion, 'new')
  assert.equal(store.pendingTag, '')
  assert.equal(timers.size, 0)
  assert.equal(progress.mock.callCount(), 0)
})

test('普通暂存仍提示重启，已确认提交也不再轮询或凭本地版本宣布成功', async (t) => {
  const { store, timers } = setup(t)
  t.mock.method(api, 'updatePending', async () => ({ pending: true, tag: 'vnext', installing: false }))
  await store.sync()
  assert.equal(store.pendingTag, 'vnext')
  assert.equal(store.justUpdated, null)
  assert.equal(timers.size, 0)
})

test('验收失败终态和取消均停止轮询', async (t) => {
  const { store, timers, tick } = setup(t)
  let pending = { pending: false, installing: true, swapFailed: false }
  t.mock.method(api, 'updatePending', async () => pending)
  await store.sync()
  pending = { pending: false, installing: false, swapFailed: true }
  await tick()
  assert.equal(store.swapFailed, true)
  assert.equal(store.justUpdated, null)
  assert.equal(timers.size, 0)
  pending = { pending: false, installing: true, swapFailed: false }
  await store.sync()
  assert.equal(timers.size, 1)
  await store.cancel()
  assert.equal(timers.size, 0)
})

test('取消后在途验收响应不能重新启动轮询', async (t) => {
  const { store, timers, tick } = setup(t)
  t.mock.method(api, 'updatePending', async () => ({ pending: false, installing: true }))
  await store.sync()
  let complete: (value: { pending: boolean; installing: boolean }) => void
  t.mock.method(api, 'updatePending', () => new Promise((resolve) => { complete = resolve }))
  await tick()
  await store.cancel()
  complete!({ pending: false, installing: true })
  await setImmediate()
  assert.equal(timers.size, 0)
})

test('既有下载轮询完成后仍刷新暂存并停止', async (t) => {
  const { store, timers, tick } = setup(t)
  let pending = { pending: false, tag: '' }
  let progress = { running: true, ok: false }
  t.mock.method(api, 'updatePending', async () => pending)
  t.mock.method(api, 'updateProgress', async () => progress)
  await store.sync()
  assert.equal(store.downloading, true)
  assert.equal(timers.size, 1)
  pending = { pending: true, tag: 'vnext' }
  progress = { running: false, ok: true }
  await tick()
  assert.equal(store.downloading, false)
  assert.equal(store.pendingTag, 'vnext')
  assert.equal(timers.size, 0)
})

test('验收轮询遇瞬时失败仍继续，恢复后读取提交成功再停', async (t) => {
  const { store, timers, tick } = setup(t)
  let calls = 0
  t.mock.method(api, 'updatePending', async () => {
    calls += 1
    if (calls === 2) throw new Error('temporary transport failure')
    return calls < 3
      ? { pending: false, installing: true }
      : { pending: false, installing: false, justUpdated: { previousVersion: 'old', currentVersion: 'new' } }
  })
  await store.sync()
  await tick()
  assert.equal(timers.size, 1)
  assert.equal(store.justUpdated, null)
  await tick()
  assert.equal(store.justUpdated?.currentVersion, 'new')
  assert.equal(timers.size, 0)
})

test('慢验收响应合并为单次读取，旧响应不会在成功后重开轮询', async (t) => {
  const { store, timers, tick } = setup(t)
  t.mock.method(api, 'updatePending', async () => ({ pending: false, installing: true }))
  await store.sync()
  let complete: (value: { pending: boolean; installing: boolean }) => void
  const slow = t.mock.method(api, 'updatePending', () => new Promise((resolve) => { complete = resolve }))
  await tick()
  await tick()
  assert.equal(slow.mock.callCount(), 1)
  complete!({ pending: false, installing: true })
  await setImmediate()
  t.mock.method(api, 'updatePending', async () => ({ pending: false, installing: false,
    justUpdated: { previousVersion: 'old', currentVersion: 'new' } }))
  await tick()
  assert.equal(store.justUpdated?.currentVersion, 'new')
  assert.equal(timers.size, 0)
})

test('确认后到达的旧成功响应不会重新设置成功提示', async (t) => {
  const { store, timers } = setup(t)
  let complete: (value: unknown) => void
  let reads = 0
  t.mock.method(api, 'updatePending', () => ++reads === 1
    ? new Promise((resolve) => { complete = resolve })
    : Promise.resolve({ pending: false, installing: false }))
  const pending = store.refreshPending()
  await store.dismissUpdated()
  complete!({ pending: false, installing: false, justUpdated: { previousVersion: 'old', currentVersion: 'new' } })
  await pending
  assert.equal(store.justUpdated, null)
  assert.equal(store.dialogOpen, false)
  assert.equal(timers.size, 0)
})


test('确认失败保留成功提示，重试成功后关闭', async (t) => {
  const { store } = setup(t)
  store.justUpdated = { previousVersion: 'old', currentVersion: 'new' }
  store.dialogOpen = true
  let unavailable = true
  t.mock.method(api, 'updateAck', async () => {
    if (unavailable) throw new Error('storage unavailable')
    return { acknowledged: true }
  })
  assert.equal(await store.dismissUpdated(), false)
  assert.equal(store.justUpdated?.currentVersion, 'new')
  assert.equal(store.dialogOpen, true)
  unavailable = false
  assert.equal(await store.dismissUpdated(), true)
  assert.equal(store.justUpdated, null)
  assert.equal(store.dialogOpen, false)
})

test('确认旧版成功报告保留随后新版下载轮询', async (t) => {
  const { store, timers, tick } = setup(t)
  t.mock.method(api, 'updatePending', async () => ({ pending: false, installing: false,
    justUpdated: { previousVersion: 'old', currentVersion: 'new' } }))
  let running = true
  t.mock.method(api, 'updateProgress', async () => ({ running, ok: !running }))
  await store.sync()
  assert.equal(store.downloading, true)
  assert.equal(timers.size, 1)
  assert.equal(await store.dismissUpdated(), true)
  assert.equal(timers.size, 1)
  running = false
  t.mock.method(api, 'updatePending', async () => ({ pending: true, tag: 'vnext', installing: false }))
  await tick()
  assert.equal(store.pendingTag, 'vnext')
  assert.equal(timers.size, 0)
})


test('确认期间下载完成的旧响应失效，确认后刷新保留下一版就绪', async (t) => {
  const { store, tick } = setup(t)
  let reads = 0
  let stale: (value: unknown) => void
  let confirmed: (value: { acknowledged: boolean }) => void
  t.mock.method(api, 'updatePending', () => {
    reads += 1
    if (reads === 2) return new Promise((resolve) => { stale = resolve })
    return Promise.resolve(reads === 1
      ? { pending: false, justUpdated: { previousVersion: 'old', currentVersion: 'new' } }
      : { pending: true, tag: 'vnext', installing: false })
  })
  let running = true
  t.mock.method(api, 'updateProgress', async () => ({ running, ok: !running }))
  t.mock.method(api, 'updateAck', () => new Promise((resolve) => { confirmed = resolve }))
  await store.sync()
  const ack = store.dismissUpdated()
  running = false
  await tick()
  confirmed!({ acknowledged: true })
  assert.equal(await ack, true)
  stale!({ pending: true, tag: 'vnext', justUpdated: { previousVersion: 'old', currentVersion: 'new' } })
  await setImmediate()
  assert.equal(store.justUpdated, null)
  assert.equal(store.pendingTag, 'vnext')
})
