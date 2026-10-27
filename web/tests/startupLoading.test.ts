import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { setImmediate } from 'node:timers/promises'
import { test } from 'node:test'
import * as Vue from 'vue'
import { compileTemplate, parse } from '@vue/compiler-sfc'
import ts from 'typescript'

const source = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const script = parse(source).descriptor.scriptSetup!.content

type Host = { children: Host[]; parent: Host | null; props: Record<string, unknown>; text: string }
const node = (): Host => ({ children: [], parent: null, props: {}, text: '' })
const renderer = Vue.createRenderer<Host, Host>({
  createElement: node, createText: node, createComment: node,
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1)
    child.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child)
  },
  remove(child) { child.parent?.children.splice(child.parent.children.indexOf(child), 1) },
  setText(target, text) { target.text = text },
  setElementText(target, text) { target.text = text },
  parentNode: target => target.parent,
  nextSibling: () => null,
  patchProp(target, key, _previous, value) { target.props[key] = value },
})

function mountDrawer(initialOpen = false) {
  const pilotStore = Vue.reactive({ open: initialOpen, game: null })
  const ast = ts.createSourceFile('App.ts', script, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const selected = ast.statements.flatMap(statement => {
    if (ts.isImportDeclaration(statement) && statement.importClause?.name?.text === 'HlPilotDrawer') {
      return ['const HlPilotDrawer = drawerImport()']
    }
    if (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(declaration =>
      ts.isIdentifier(declaration.name) &&
      (declaration.name.text === 'HlPilotDrawer' || declaration.name.text === 'pilotStore' ||
        declaration.name.text.startsWith('pilotDrawer')))) {
      return [statement.getText(ast).replace(/import\(['"]@\/components\/business\/HlPilotDrawer.vue['"]\)/,
        'Promise.resolve(drawerImport())')]
    }
    if (ts.isExpressionStatement(statement) && statement.getText(ast).startsWith('watch(') &&
      statement.getText(ast).includes('pilotDrawer')) return [statement.getText(ast)]
    return []
  })
  const declarationNames = [...selected.join('\n').matchAll(/const (\w+)\s*=/g)].map(match => match[1])
  const setup = new Function('defineAsyncComponent', 'ref', 'watch', 'usePilotStore', 'drawerImport',
    `${selected.join('\n')}\nreturn { ${declarationNames.join(',')} }`)
  const drawerTag = source.match(/<HlPilotDrawer\b[^>]*\/>/)![0]
  const template = compileTemplate({
    source: drawerTag,
    filename: 'App.vue', id: 'startup-loading',
    compilerOptions: { bindingMetadata: Object.fromEntries(declarationNames.map(name =>
      [name, name.startsWith('pilotDrawer') ? 'setup-ref' : 'setup-const'])) },
  })
  assert.deepEqual(template.errors, [])
  const renderCode = template.code
    .replace(/import \{([^}]+)\} from "vue"/, (_match, names: string) =>
      `const {${names.replace(/\bas\b/g, ':')}} = Vue`)
    .replace('export function render', 'return function render')
  const render = new Function('Vue', renderCode)(Vue)
  let loads = 0
  let mounts = 0
  const drafts: Vue.Ref<string>[] = []
  const Drawer = Vue.defineComponent({
    props: ['modelValue', 'game'],
    setup(props) {
      mounts++
      const draft = Vue.ref('')
      drafts.push(draft)
      return () => Vue.h('drawer', { open: props.modelValue, draft: draft.value })
    },
  })
  const app = renderer.createApp(Vue.defineComponent({
    setup: () => setup(Vue.defineAsyncComponent, Vue.ref, Vue.watch, () => pilotStore,
      () => { loads++; return Drawer }),
    render,
  }))
  app.mount(node())
  return { pilotStore, drafts, app, loads: () => loads, mounts: () => mounts }
}

async function flushDrawer() {
  await Vue.nextTick()
  await setImmediate()
  await Vue.nextTick()
}

test('closed pilot drawer loads on demand and retains its instance after reopening', async () => {
  const fixture = mountDrawer()
  try {
    await flushDrawer()
    assert.equal(fixture.loads(), 0, 'closed drawer must not load its chart dependencies')
    assert.equal(fixture.mounts(), 0)
    fixture.pilotStore.open = true
    await flushDrawer()
    assert.equal(fixture.loads(), 1)
    assert.equal(fixture.mounts(), 1)
    fixture.drafts[0].value = 'unfinished message'
    fixture.pilotStore.open = false
    await flushDrawer()
    fixture.pilotStore.open = true
    await flushDrawer()
    assert.equal(fixture.loads(), 1)
    assert.equal(fixture.mounts(), 1)
    assert.equal(fixture.drafts[0].value, 'unfinished message')
  } finally { fixture.app.unmount() }
})

test('pilot already requested before root mounting remains available', async () => {
  const fixture = mountDrawer(true)
  try {
    await flushDrawer()
    assert.equal(fixture.loads(), 1)
    assert.equal(fixture.mounts(), 1)
  } finally { fixture.app.unmount() }
})

test('root startup does not prefetch the achievements page', () => {
  assert.doesNotMatch(script, /import\(['"]\.\/views\/achievements\/Index.vue['"]\)/)
})
