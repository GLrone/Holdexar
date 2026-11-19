import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { parse } from '@vue/compiler-sfc'
import ts from 'typescript'

// 领航台点击委托：正文 http(s) 外链窗外打开（不进应用窗口导航），#fragment 与相对链接维持默认

function clickHandler() {
  const source = readFileSync(new URL('../src/components/business/HlPilotConsole.vue', import.meta.url), 'utf8')
  const script = parse(source).descriptor.scriptSetup!.content
  const ast = ts.createSourceFile('Console.ts', script, ts.ScriptTarget.Latest, true)
  const declarations = ast.statements
    .filter((statement) => ts.isFunctionDeclaration(statement) && statement.name?.text === 'onTurnsClick')
    .map((statement) => statement.getText(ast))
  assert.equal(declarations.length, 1)
  const code = ts.transpileModule(declarations.join('\n'), {
    compilerOptions: { target: ts.ScriptTarget.ESNext },
  }).outputText
  const opened: string[] = []
  const copied: string[] = []
  const window = { open: (href: string) => { opened.push(href) } }
  const onCopyText = async (text: string) => { copied.push(text) }
  const quoteToComposer = () => {}
  const run = new Function('window', 'onCopyText', 'quoteToComposer', `${code}\nreturn onTurnsClick`)(
    window, onCopyText, quoteToComposer,
  ) as (e: unknown) => void
  return { run, opened, copied }
}

function fakeEvent(map: Record<string, unknown>) {
  const prevented: number[] = []
  const e = {
    target: { closest: (selector: string) => map[selector] ?? null },
    preventDefault: () => { prevented.push(1) },
  }
  return { e, prevented }
}

test('正文 http(s) 外链点击：窗外打开并拦截应用窗口内导航', () => {
  const { run, opened } = clickHandler()
  const { e, prevented } = fakeEvent({
    'a[href]': { getAttribute: (key: string) => (key === 'href' ? 'https://evil.example/phish' : null) },
  })
  run(e)
  assert.equal(prevented.length, 1)
  assert.deepEqual(opened, ['https://evil.example/phish'])
})

test('#fragment 与站内相对链接维持默认行为（不拦截不外开）', () => {
  const { run, opened } = clickHandler()
  const frag = fakeEvent({ 'a[href]': { getAttribute: () => '#section' } })
  run(frag.e)
  assert.equal(frag.prevented.length, 0)
  assert.deepEqual(opened, [])
  const rel = fakeEvent({ 'a[href]': { getAttribute: () => '/bills' } })
  run(rel.e)
  assert.equal(rel.prevented.length, 0)
  assert.deepEqual(opened, [])
})

test('非锚点动作（代码块复制钮）不受锚点分支影响', () => {
  const { run, copied, opened } = clickHandler()
  const { e } = fakeEvent({
    'a[href]': null,
    '.pilot-code-copy': {
      closest: (selector: string) => (selector === '.pilot-code'
        ? { querySelector: () => ({ textContent: 'const x = 1' }) }
        : null),
    },
  })
  run(e)
  assert.deepEqual(copied, ['const x = 1'])
  assert.deepEqual(opened, [])
})
