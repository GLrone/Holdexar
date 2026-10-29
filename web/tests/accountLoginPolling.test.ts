import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { parse } from '@vue/compiler-sfc'
import { computed, ref } from 'vue'
import ts from 'typescript'

function polling(initialState: string | null) {
  const source = readFileSync(new URL('../src/views/settings/Index.vue', import.meta.url), 'utf8')
  const script = parse(source).descriptor.scriptSetup!.content
  const ast = ts.createSourceFile('Settings.ts', script, ts.ScriptTarget.Latest, true)
  const names = new Set(['startLoginPolling', 'stopLoginPolling', 'refreshLoginState'])
  const declarations = ast.statements.filter(statement =>
    (ts.isFunctionDeclaration(statement) && names.has(statement.name?.text ?? '')) ||
    (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(declaration =>
      ts.isIdentifier(declaration.name) && ['LOGIN_BUSY_STATES', 'loginActive'].includes(declaration.name.text))))
    .map(statement => statement.getText(ast))
  assert.equal(declarations.length, 5)
  const code = ts.transpileModule(`let loginTimer = null;\n${declarations.join('\n')}`, {
    compilerOptions: { target: ts.ScriptTarget.ESNext },
  }).outputText

  let callback: unknown
  const cleared: number[] = []
  let reads = 0
  let accountLoads = 0
  const timeouts: unknown[] = []
  const messages: string[] = []
  const response = { state: 'done' }
  const bindings = {
    computed,
    window: {
      setInterval(handler: unknown) { callback = handler; return 7 },
      clearInterval(id: number) { cleared.push(id) },
      setTimeout(handler: unknown) { timeouts.push(handler); return 8 },
    },
    accountLoginApi: { async status() { reads++; return { ...response } } },
    loginState: ref<{ state: string } | null>(initialState ? { state: initialState } : null),
    loginCodeMode: { value: true },
    loginAccount: { value: 'test' },
    loginCodeInput: { value: '' },
    loginCodeError: { value: '' },
    loginCodeAccepted: { value: true },
    accountStore: { async load() { accountLoads++ } },
    message: { success(value: string) { messages.push(value) } },
    t: (key: string) => key,
  }
  const actions = new Function('bindings',
    `const { ${Object.keys(bindings).join(',')} } = bindings;\n${code}\nreturn { startLoginPolling, refreshLoginState }`)(bindings)
  return { actions, bindings, response, cleared, messages, timeouts,
    callback: () => callback, reads: () => reads, accountLoads: () => accountLoads }
}

test('登录轮询调用刷新函数，成功后同步状态并停止等待', async () => {
  const h = polling('awaiting_confirmation')
  h.actions.startLoginPolling()
  assert.equal(typeof h.callback(), 'function')
  await (h.callback() as () => Promise<void>)()
  assert.equal(h.reads(), 1)
  assert.equal(h.bindings.loginState.value?.state, 'done')
  assert.equal(h.bindings.loginCodeMode.value, false)
  assert.equal(h.accountLoads(), 1)
  assert.deepEqual(h.cleared, [7])
  assert.deepEqual(h.messages, ['settings.steam.loginDone'])
  await h.actions.refreshLoginState()
  assert.equal(h.messages.length, 1)
  assert.equal(h.accountLoads(), 1)
})

test('已经完成的登录在多次重新进入页面时不重放成功条幅或提示', async () => {
  for (let visit = 0; visit < 2; visit++) {
    const h = polling(null)
    await h.actions.refreshLoginState()
    assert.equal(h.bindings.loginState.value, null)
    assert.deepEqual(h.messages, [])
    assert.equal(h.accountLoads(), 0)
    assert.equal(h.timeouts.length, 0)
  }
})

test('切页恢复进行中的登录仍轮询，并在完成时提示一次', async () => {
  const h = polling(null)
  h.response.state = 'awaiting_confirmation'
  await h.actions.refreshLoginState()
  assert.equal(h.bindings.loginState.value?.state, 'awaiting_confirmation')
  assert.deepEqual(h.messages, [])
  h.actions.startLoginPolling()
  h.response.state = 'done'
  await (h.callback() as () => Promise<void>)()
  assert.equal(h.bindings.loginState.value?.state, 'done')
  assert.equal(h.accountLoads(), 1)
  assert.deepEqual(h.messages, ['settings.steam.loginDone'])
})
