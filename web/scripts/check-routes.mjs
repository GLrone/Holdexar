/**
 * 路由 → 视图悬空引用校验（提交层防线）。
 *
 * 从路由文件提取全部 `.vue` 导入说明符（静态 import 与动态 import() 通用），
 * 断言目标文件存在——「挂了路由但视图未建」的半成品不允许提交进仓库。
 *
 * 与 vite 插件 stub-missing-views 的分工：
 *   插件 = 工作树层（未提交的并行施工不炸他人构建，占位顶替 + 警告）
 *   本脚本 = 提交层（build 链 + pre-commit，悬空引用拦截入库）
 *
 * 用法：node scripts/check-routes.mjs [routerFile]
 *       （默认 src/router/index.ts；传参供测试夹具用）
 */
import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const webRoot = fileURLToPath(new URL('..', import.meta.url))
const routerFile = resolve(webRoot, process.argv[2] ?? 'src/router/index.ts')

if (!existsSync(routerFile)) {
  console.error(`✗ 路由文件不存在: ${routerFile}`)
  process.exit(1)
}

const text = readFileSync(routerFile, 'utf8')
const specs = [...new Set([...text.matchAll(/['"]([^'"]+\.vue)['"]/g)].map((m) => m[1]))]

if (specs.length === 0) {
  console.error(`✗ 路由文件中未发现任何 .vue 引用（解析规则失效或文件非路由）: ${routerFile}`)
  process.exit(1)
}

const missing = []
for (const spec of specs) {
  let target = null
  if (spec.startsWith('@/')) target = resolve(webRoot, 'src', spec.slice(2))
  else if (spec.startsWith('.')) target = resolve(dirname(routerFile), spec)
  else if (spec.startsWith('src/')) target = resolve(webRoot, spec)
  if (!target || !existsSync(target)) missing.push(spec)
}

if (missing.length > 0) {
  console.error(`✗ 路由悬空引用 ${missing.length} 处（提交被拦截）：`)
  for (const spec of missing) console.error(`    ${spec}`)
  console.error('  施工 SOP：挂路由与视图必须同批落地；')
  console.error('  中途停工二选一——撤回路由，或提交「施工中」占位视图。')
  process.exit(1)
}

console.log(`✓ 路由引用完整：${specs.length} 个视图全部存在`)
