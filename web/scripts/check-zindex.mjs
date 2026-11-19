/**
 * 浮层 z-index 阶梯校验（提交层防线）。
 *
 * 全站浮层共用一条 z 阶梯，
 * 层间大小关系是全站约定：任何「抄一个够大的魔数」都会静默压过幕布/弹层。
 * 本脚本冻结现役取值清单：出现清单外的 z-index 即报错，新档位必须先评审
 * 确认再落码（与 eslint LEGACY_FILES / 例外登记同哲学）。
 *
 * 覆盖两种写法：CSS 声明 `z-index: N` 与 Tailwind 任意值 `z-[N]`。
 *
 * 用法：node scripts/check-zindex.mjs
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const webRoot = fileURLToPath(new URL('..', import.meta.url))
const srcRoot = join(webRoot, 'src')

/** 现役取值清单（新档位先核对各层基准再加进这里） */
const ALLOWED = new Set([
  '-1', // 内部层叠（BorderGlow 置底）
  '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '12', // 页内卡片与内部层叠
  '20', '30', '50', '60', '90', '95', // 吸顶工具栏 / 卡内浮块
  '100', '101', '102', // 抽屉遮罩 / 抽屉 / 对话框
  '800', // 卡内锚定浮层
  '990', // 卡内 GPW 弹窗
  '1000', // teleport 弹层（必须高于抽屉）
  '1200', '1500', '1600', // 锚定气泡 / 全局模态与菜单幕布带
  '1990', // 区块导航悬浮条
  '2000', // 导览幕布 / 关窗幕布
  '9999', '10000', '10001', '10003', '10004', '10005', // toast/灵动岛/赠礼系统带
  '99999', // 悬停工具窗（pointer-events:none）
])

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    const st = statSync(path)
    if (st.isDirectory()) yield* walk(path)
    else if (/\.(vue|css|scss|ts)$/.test(name)) yield path
  }
}

const violations = []
for (const file of walk(srcRoot)) {
  const text = readFileSync(file, 'utf8')
  const rel = relative(webRoot, file).replaceAll('\\', '/')
  const lines = text.split('\n')
  lines.forEach((line, i) => {
    for (const m of line.matchAll(/z-index:\s*(-?\d+)|z-\[(-?\d+)\]/g)) {
      const value = m[1] ?? m[2]
      if (!ALLOWED.has(value)) {
        violations.push(`${rel}:${i + 1}: z-index ${value} 不在浮层阶梯清单`)
      }
    }
  })
}

if (violations.length > 0) {
  console.error('✗ 浮层 z-index 越档：请对照站内各层基准选档，')
  console.error('  确需新档位的先登记再同步 web/scripts/check-zindex.mjs 的 ALLOWED 清单。')
  for (const v of violations) console.error(`  ${v}`)
  process.exit(1)
}
console.log(`[check-zindex] ✓ 浮层阶梯全部在册`)
