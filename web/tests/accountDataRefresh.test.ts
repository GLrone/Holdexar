import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { createPinia, defineStore, setActivePinia } from 'pinia'
import { computed, nextTick, reactive, ref, watch } from 'vue'
import ts from 'typescript'
import { parse } from '@vue/compiler-sfc'

function store(file: 'ownedLib' | 'familyLib', api: object) {
  setActivePinia(createPinia())
  const account = reactive({ accounts: [{ steam_id: 'A', bound_at: 'first', game_count: 1, wishlist_count: 0 }],
    active: { steam_id: 'A' } })
  const source = readFileSync(new URL(`../src/stores/${file}.ts`, import.meta.url), 'utf8')
  const ast = ts.createSourceFile('store.ts', source, ts.ScriptTarget.Latest, true)
  const code = ast.statements.filter(s => !ts.isImportDeclaration(s)).map(s => s.getText(ast))
    .join('\n').replace(/^export /gm, '')
  const js = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ESNext } }).outputText
  const bindings = { defineStore, ref, computed, watch, normalizeAvatarUrl: (v: string) => v,
    useAccountStore: () => account, useI18n: () => ({ t: (k: string) => k }),
    watchPoolApi: api, familyApi: api, message: {} }
  const result = new Function(...Object.keys(bindings), `${js}\nreturn ${file === 'ownedLib' ? 'useOwnedLibStore' : 'useFamilyStore'}()`)(...Object.values(bindings))
  return { account, result }
}

test('后台取数更新账号计数后，已经打开的游戏库自动读取新结果', async () => {
  let reads = 0
  const { account, result } = store('ownedLib', { async ownedLibrary() {
    reads++
    return { accounts: [], games: [{ appid: reads }] }
  } })
  await result.load()
  account.accounts[0]!.game_count = 2
  await nextTick()
  await nextTick()
  assert.equal(reads, 2)
  assert.equal(result.games[0].appid, 2)
})

test('家庭库切号时丢弃旧账号在途响应，并接着读取新账号', async () => {
  const requests: string[] = []
  let resolveOld!: (value: object) => void
  const { result } = store('familyLib', { library(sid: string) {
    requests.push(sid)
    return sid === 'A' ? new Promise(resolve => { resolveOld = resolve })
      : Promise.resolve({ members: [], games: [{ appid: 'B' }], memberPlay: {} })
  } })
  const loading = result.setActiveGroup('A')
  await result.setActiveGroup('B')
  resolveOld({ members: [], games: [{ appid: 'A' }], memberPlay: {} })
  await loading
  assert.deepEqual(requests, ['A', 'B'])
  assert.equal(result.games[0].appid, 'B')
})

test('游戏库、家庭与账单读取不复用旧的请求时间窗缓存', async () => {
  const source = readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8')
  const ast = ts.createSourceFile('client.ts', source, ts.ScriptTarget.Latest, true)
  const code = ast.statements.filter(s => !ts.isImportDeclaration(s)).map(s => s.getText(ast))
    .join('\n').replace(/^export /gm, '')
  const js = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ESNext } }).outputText
  let revision = 0
  const api = new Function('fetch', `${js}\nreturn { watchPoolApi, familyApi, billsApi }`)(
    async () => Response.json({ revision: ++revision }))
  for (const read of [api.watchPoolApi.ownedLibrary, api.familyApi.library, api.billsApi.listImports]) {
    const first = await read()
    const second = await read()
    assert.notEqual(second.revision, first.revision)
  }
})

test('进入账单页时尚未开始抓取，稍后账号账单落库仍自动刷新页面', async () => {
  const source = readFileSync(new URL('../src/views/bills/Index.vue', import.meta.url), 'utf8')
  const ast = ts.createSourceFile('bills.ts', parse(source).descriptor.scriptSetup!.content, ts.ScriptTarget.Latest, true)
  const statements = ast.statements.filter(s =>
    ts.isFunctionDeclaration(s) && ['loadImports', 'loadOverview'].includes(s.name?.text ?? '') ||
    ts.isExpressionStatement(s) && ts.isCallExpression(s.expression) &&
      s.expression.expression.getText(ast) === 'watch' && s.getText(ast).includes('bills_synced_at'))
  assert.equal(statements.length, 3)
  const js = ts.transpileModule(statements.map(s => s.getText(ast)).join('\n'), {
    compilerOptions: { target: ts.ScriptTarget.ESNext },
  }).outputText
  const accountStore = reactive({ accounts: [{ steam_id: 'B', bills_synced_at: null as string | null }],
    active: { steam_id: 'B' } })
  let rows = [{ id: 1, steamId: null as string | null }]
  const overview = ref<{ id: number; steamId: string | null } | null>(null)
  const bindings = { watch, imports: ref([]), activeId: ref<number | null>(null), overview, loading: ref(false), accountStore,
    billsApi: { async listImports() { return { imports: rows } }, async overview(id: number) { return rows.find(r => r.id === id) } },
    message: { error: assert.fail } }
  const actions = new Function(...Object.keys(bindings), `${js}\nreturn {loadImports,loadOverview}`)(...Object.values(bindings))
  await actions.loadImports()
  await actions.loadOverview()
  assert.equal(overview.value?.id, 1)
  rows = [{ id: 2, steamId: 'B' }, ...rows]
  accountStore.accounts[0]!.bills_synced_at = 'completed'
  await new Promise(setImmediate)
  assert.equal(overview.value?.id, 2)
  assert.equal(overview.value?.steamId, 'B')
})
