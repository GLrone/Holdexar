import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'

test('登录轮询调用刷新函数，成功后同步状态并停止等待', async () => {
  const source = readFileSync(new URL('../src/views/settings/Index.vue', import.meta.url), 'utf8')
  const script = parse(source).descriptor.scriptSetup!.content
  const ast = ts.createSourceFile('Settings.ts', script, ts.ScriptTarget.Latest, true)
  const names = new Set(['startLoginPolling', 'stopLoginPolling', 'refreshLoginState'])
  const functions = ast.statements.filter(statement => ts.isFunctionDeclaration(statement) &&
    names.has(statement.name?.text ?? '')).map(statement => statement.getText(ast))
  assert.equal(functions.length, 3)
  const code = ts.transpileModule(`let loginTimer = null;\n${functions.join('\n')}`, {
    compilerOptions: { target: ts.ScriptTarget.ESNext },
  }).outputText

  let callback: unknown
  const cleared: number[] = []
  let reads = 0
  let accountLoads = 0
  const messages: string[] = []
  const bindings = {
    window: {
      setInterval(handler: unknown) { callback = handler; return 7 },
      clearInterval(id: number) { cleared.push(id) },
      setTimeout() { return 8 },
    },
    accountLoginApi: { async status() { reads++; return { state: 'done' } } },
    loginState: { value: { state: 'awaiting_confirmation' } },
    loginCodeMode: { value: true },
    loginAccount: { value: 'test' },
    loginCodeInput: { value: '' },
    loginCodeError: { value: '' },
    loginCodeAccepted: { value: true },
    accountStore: { async load() { accountLoads++ } },
    message: { success(value: string) { messages.push(value) } },
    t: (key: string) => key,
  }
  const start = new Function('bindings',
    `const { ${Object.keys(bindings).join(',')} } = bindings;\n${code}\nreturn startLoginPolling`)(bindings)
  start()
  assert.equal(typeof callback, 'function')
  await (callback as () => Promise<void>)()
  assert.equal(reads, 1)
  assert.equal(bindings.loginState.value.state, 'done')
  assert.equal(bindings.loginCodeMode.value, false)
  assert.equal(accountLoads, 1)
  assert.deepEqual(cleared, [7])
  assert.deepEqual(messages, ['settings.steam.loginDone'])
})
