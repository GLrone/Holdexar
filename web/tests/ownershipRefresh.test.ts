import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import ts from 'typescript'
import { defineStore, createPinia, setActivePinia } from 'pinia'
import { reactive, ref, watch } from 'vue'

/* ownership store：dataEpoch 纪元推进 → 缓存整批重取（N11 失效通道回归） */

function ownershipStoreWith(api: Record<string, unknown>, dataEpoch: ReturnType<typeof ref<number>>) {
  ;(globalThis as unknown as { window: unknown }).window = {
    setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms),
    clearTimeout: (id: number) => clearTimeout(id),
  }
  setActivePinia(createPinia())
  const source = readFileSync(new URL('../src/stores/ownership.ts', import.meta.url), 'utf8')
  const ast = ts.createSourceFile('ownership.ts', source, ts.ScriptTarget.Latest, true)
  const code = ast.statements.filter((s) => !ts.isImportDeclaration(s)).map((s) => s.getText(ast))
    .join('\n').replace(/^export /gm, '')
  const js = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.Latest } }).outputText
  const factory = new Function(
    'defineStore', 'ref', 'watch', 'ownershipApi', 'useCrawlStatusStore',
    `${js}\nreturn useOwnershipStore()`,
  )
  return factory(defineStore, ref, watch, api, () => reactive({ dataEpoch }))
}

const infoOf = (type: string) => ({ type, owners: ['S1'] })

test('ownership.ensure：同帧合并为一次批量拉取', async () => {
  const batches: number[][] = []
  const store = ownershipStoreWith({
    async batch(appids: number[]) {
      batches.push([...appids])
      return { ownerships: Object.fromEntries(appids.map((id) => [id, infoOf('owned')])) }
    },
  }, ref(0))
  store.ensure(1)
  store.ensure(2)
  store.ensure(1)
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.deepEqual(batches, [[1, 2]])
  assert.equal(store.map[1]!.type, 'owned')
})

test('ownership.refresh：dataEpoch 推进即清空缓存并按原清单重拉（徽章响应式跟随）', async () => {
  let type = 'wishlist'
  const batches: number[][] = []
  const epoch = ref(0)
  const store = ownershipStoreWith({
    async batch(appids: number[]) {
      batches.push([...appids])
      return { ownerships: Object.fromEntries(appids.map((id) => [id, infoOf(type)])) }
    },
  }, epoch)
  store.ensure(7)
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.equal(store.map[7]!.type, 'wishlist')

  type = 'owned' // 账户同步后归属事实变化
  store.ensure(7) // 已缓存：ensure 不再发起
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.equal(store.map[7]!.type, 'wishlist', '无失效通道时旧值常驻')

  epoch.value++ // 任务终态推进纪元（job 终态 / 价格轮收尾）
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.equal(store.map[7]!.type, 'owned', '纪元推进后徽章拿到新归属')
  assert.deepEqual(batches, [[7], [7]])
})

test('ownership.refresh：空缓存时纪元推进不发起请求', async () => {
  const batches: number[][] = []
  const epoch = ref(0)
  const store = ownershipStoreWith({
    async batch(appids: number[]) {
      batches.push([...appids])
      return { ownerships: {} }
    },
  }, epoch)
  epoch.value++
  await new Promise((resolve) => setTimeout(resolve, 60))
  assert.deepEqual(batches, [])
})
