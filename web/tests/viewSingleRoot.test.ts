import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { test } from 'node:test'
import { parse } from '@vue/compiler-sfc'
import { fileURLToPath } from 'node:url'

/* 路由视图挂在 <Transition mode="out-in">（App.vue）内，多根 fragment 会让视图切换永久卡死；
   此测试锁死「router 挂载的每个视图（views/<x>/Index.vue 与 _stub/Missing.vue，见 router/index.ts）
   模板恰好一个元素根」的契约。非路由子组件不受此约束。 */
const viewsDir = new URL('../src/views/', import.meta.url)

function routeViewFiles(): URL[] {
  const files: URL[] = []
  for (const entry of readdirSync(viewsDir)) {
    const dir = new URL(`${entry}/`, viewsDir)
    if (entry === '_stub') {
      files.push(new URL('Missing.vue', dir))
      continue
    }
    files.push(new URL('Index.vue', dir))
  }
  return files
}

test('路由视图模板必须是单根（Transition out-in 契约）', () => {
  const multiRoot: string[] = []
  for (const file of routeViewFiles()) {
    let source: string
    try {
      source = readFileSync(file, 'utf8')
    } catch {
      continue // 视图文件未建时由 vite stub 兜底（check-routes 管悬空引用），这里只测存在的
    }
    const { descriptor } = parse(source, { filename: fileURLToPath(file) })
    const ast = descriptor.template?.ast
    if (!ast) continue
    const real = ast.children.filter(
      (n) => !(n.type === 2 && !('content' in n && String(n.content).trim())),
    )
    if (real.length !== 1 || real[0].type !== 1) multiRoot.push(fileURLToPath(file))
  }
  assert.deepEqual(
    multiRoot,
    [],
    `多根视图会让 router-view 过渡永久卡死，必须包一层单根：\n${multiRoot.join('\n')}`,
  )
})
