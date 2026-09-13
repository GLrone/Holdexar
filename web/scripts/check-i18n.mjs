#!/usr/bin/env node
/* 词典完整性门禁 —— 与 check-routes.mjs / check-colors.mjs 同构，走 `npm run build` 链。
 *
 * 为什么需要它：`t()` 的类型安全（MessageKey 字面量联合、拼错 key 即编译失败）
 * 只在**编辑器**里生效——`npm run build` 跑的是 `vite build`，而 vite 只转译不做
 * 类型检查（`vue-tsc` 未装，`tsc --noEmit` 也因 tsconfig 的 baseUrl 弃用告警跑不起来）。
 * 于是拼错的 key 会一路过四层门禁进产物，运行时静默渲染成「key 原文」——
 * 用户看到的是 `gameCard.badge.owned` 而不是「已拥有」，而且**不报任何错**。
 * 这个脚本就是补上那道缺失的校验，把类型层的保证落到构建链上。
 *
 * 八项判据（全部 exit 1）：
 *   ① 重复 key —— 词典是 `{ ...a, ...b }` 展开的，同名后写的静默覆盖先写的；
 *   ② 调用点 `t('x.y')` 引用了不存在的 key（拼错、或改词典时漏改调用点）；
 *   ③ 常量表里的 key 值（`badgeKey: 'x.y'`）引用了不存在的 key —— ② 看不见
 *      这类（它不在 `t(...)` 里），而它恰恰是冻结陷阱的标准解法，见下方注释；
 *   ④ `data-section` 锚点值（`data-section="x.y"`）引用了不存在的 key —— 同理
 *      ② 看不见；写错时 HlSectionRail 的气泡直接显示 key 原文，见下方注释；
 *   ⑤ zh-CN 有、en 没有的缺译 —— 英文界面下会回退成中文，是用户可见的缺陷；
 *   ⑥ en 有、zh-CN 没有的孤儿词条（类型层本该拦住，见上：类型层没跑）；
 *   ⑦ 词条值里的 `{name}` 没被调用点供上 —— ⑤⑥ 只看 key 在不在，看不见值里的
 *      占位符。少了参数就少一个数（`共 {n} 个` → `共 个`），多写一个则原样渲染
 *      出 `{n}` 字面量，**两种都不报错**，只能在这里拦。判的是「值要的 ⊆ 调用点
 *      给的」，不是「中英两种语言的占位符集合相等」——后者会误报一种正确写法：
 *      同一 key 的两种语言合理地要不同的数据（`rates.chart.yAxis` 中文要
 *      `{currency}` 显示「人民币」、英文要 `{code}` 显示 "CNY"）。
 *   ⑧ en 词典的**值**里还有中文 —— ⑤⑥ 判的是 key 在不在两边都有，判不出
 *      「key 齐了、英文值却是从 zh 粘过来的」。这是双语迁移里最容易发生、又最难
 *      靠肉眼发现的翻译事故：界面切到英文，那一条照旧显示中文，而所有门禁全绿。
 *      与 eslint 的 no-hardcoded-cjk 不重复：那条查的是**组件里的字面量**，
 *      词典目录被它整目录豁免（那是中文的正确落点），所以词典自身的值无人看守。
 *      白名单只有 `shell.lang.*` 三条——语言切换器的标签**有意**用目标语言自己的
 *      写法（"Switch to 中文" 是写给中文用户看的），是全项目唯一一处中文出现在
 *      英文词典里是正确的地方，逐条列出并在下方注明理由。
 *
 * 关于 ③ 为何是 error 而非 warn：运行时回退（当前语言 → zh-CN → key 原文）是**兜底**，
 * 不是工作流。本项目的迁移粒度是「一个文件迁完」，一个文件只要有 key 就该同期补齐
 * 英文；没有「合法缺译」的中间态。想先写中文、稍后补译的，构建会拦下你——
 * 这正是红线该有的样子。
 *
 * 不做的事：动态 key（`t(SOME_MAP[x])`、``t(`a.${b}`)``）静态判不了，只计数不判。
 *
 * 用法：node scripts/check-i18n.mjs [--report]
 *       --report 只列不判（用于盘点），不带参数则违规即 exit 1。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'

const SRC = 'src'
const ZH_DIR = join(SRC, 'locales', 'zh-CN')
const EN_DIR = join(SRC, 'locales', 'en')

const reportOnly = process.argv.includes('--report')

/** 递归收集文件（只认 .vue / .ts） */
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(vue|ts)$/.test(name)) out.push(p)
  }
  return out
}

/**
 * 从词典模块里取 key。
 *
 * 只认**行首**的 `'key':`——这是本目录的排版约定，也是唯一可靠的判据：
 * 词条值里出现 `xxx: 'yyy'` 这种内容的可能性不为零（长句、示例文本），
 * 不做行首锚定就会把值的片段当成 key 收进来，然后报一堆不存在的「缺译」。
 */
function collectKeys(dir) {
  const keys = new Map() // key → 来源文件
  const values = new Map() // key → 词条值（原文，未做任何转义还原之外的加工）
  const dups = []
  for (const file of walk(dir)) {
    if (file.endsWith(join('', 'index.ts'))) continue
    const src = readFileSync(file, 'utf8')
    for (const m of src.matchAll(/^[ \t]*(['"])([^'"]+)\1[ \t]*:/gm)) {
      const key = m[2]
      const rel = relative('.', file).split(sep).join('/')
      if (keys.has(key)) dups.push({ key, a: keys.get(key), b: rel })
      else {
        keys.set(key, rel)
        values.set(key, readValue(src, m.index + m[0].length))
      }
    }
  }
  return { keys, values, dups }
}

/**
 * 从冒号之后读出词条的值（跳过换行，取紧随其后的那个字符串字面量）。
 *
 * 值可能换行（`'key':\n  'value',` 是 en/ 里的常见排版），所以不能按行取；
 * 也不能用贪婪正则——值里出现引号的情况（`'a「b」c'`）会把匹配拉过头。
 * 逐个字符走到配对的引号，反斜杠跳过下一个字符，是唯一稳妥的读法。
 * 取不到（值不是字符串字面量）返回 null，占位符判据跳过该条。
 */
function readValue(src, from) {
  let i = from
  while (i < src.length && /[\s:]/.test(src[i])) i += 1
  const q = src[i]
  if (q !== "'" && q !== '"' && q !== '`') return null
  const end = skipString(src, i)
  if (end === -1) return null
  return src.slice(i + 1, end).replace(/\\(.)/g, '$1')
}

/** 从起始引号走到配对的结束引号，返回其下标；未闭合返回 -1 */
function skipString(src, start) {
  const q = src[start]
  let i = start + 1
  while (i < src.length) {
    if (src[i] === '\\') {
      i += 2
      continue
    }
    if (src[i] === q) return i
    i += 1
  }
  return -1
}

/**
 * 读一个对象字面量的**顶层键名**（`{ n: 1, name: x }` → `['n', 'name']`）。
 *
 * 用来知道某个 `t()` 调用点到底供了哪些参数。按括号深度走，字符串整体跳过——
 * 否则值里的 `}`（如 `'{a}'` 或 `'😀'` 之类的转义）会把深度算错。
 * 遇到展开（`...rest`）就返回 null：那种情况静态判不了，按本文件一贯的
 * 「动态不判」处理，不猜。
 */
function readObjectKeys(src, start) {
  const keys = []
  let depth = 0
  let expectKey = false
  let i = start
  while (i < src.length) {
    const c = src[i]
    if (c === "'" || c === '"' || c === '`') {
      const end = skipString(src, i)
      if (end === -1) return null
      if (depth === 1 && expectKey) keys.push(src.slice(i + 1, end).replace(/\\(.)/g, '$1'))
      i = end + 1
      expectKey = false
      continue
    }
    if (c === '{') {
      depth += 1
      expectKey = true
      i += 1
      continue
    }
    if (c === '}') {
      depth -= 1
      i += 1
      if (depth === 0) return keys
      expectKey = false
      continue
    }
    if (c === '.' && src.startsWith('...', i)) return null
    if (c === ',') {
      i += 1
      expectKey = true
      continue
    }
    if (c === ':') {
      i += 1
      if (depth === 1) expectKey = false
      continue
    }
    if (depth === 1 && expectKey && /[A-Za-z_$]/.test(c)) {
      const m = /^[A-Za-z_$][\w$]*/.exec(src.slice(i))
      keys.push(m[0])
      i += m[0].length
      expectKey = false
      continue
    }
    i += 1
  }
  return null
}

const zh = collectKeys(ZH_DIR)
const en = collectKeys(EN_DIR)

/**
 * 抹掉注释再扫调用点。
 *
 * 不这么做的话，注释里为举例写的 `t('xxx.yyy')` 会被当成真实调用点——本文件的
 * 头注就在讲这件事，一写就自报「调用了不存在的 key」。而「在注释里举 key 的例」
 * 恰恰是这套东西希望人做的（说明某个 anchor 为什么用某个 key），所以必须让
 * 注释成为不可见区域，而不是要求大家别在注释里写这种字面量。
 *
 * 用替换而非删除：保留长度，行号才不会漂。三种都抹：`/* *\/`、`//`、模板的 `<!-- -->`。
 * 朴素正则对「字符串里含 //」（如 URL）会过度抹除，但那只会造成漏报（少查几处），
 * 不会误报——对门禁来说漏报可接受、误报不可接受。
 */
function stripComments(src) {
  const blank = (m) => m.replace(/[^\n]/g, ' ')
  return src
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/<!--[\s\S]*?-->/g, blank)
    .replace(/(^|[^:\\])\/\/[^\n]*/g, (m, p) => p + blank(m.slice(p.length)))
}

/* ── 扫描调用点 ── */
const LITERAL_CALL = /\bt\(\s*(['"])([^'"]+)\1/g
const DYNAMIC_CALL = /\bt\(\s*(?:`|[A-Za-z_$])/g
/** `t('key'` 之后紧跟的是 `)`（无参）还是 `,`（要看参数）——供占位符判据用 */
const CALL_ARGS = /\bt\(\s*(['"])([^'"]+)\1\s*(\)|,)/g

/**
 * 常量表里的词条 key —— `badgeKey: 'gameCard.status.owned'` 这类。
 *
 * 为什么必须单独查：把 key 存进常量表、渲染期再 `t(常量表[x].badgeKey)` 是
 * **冻结陷阱的标准解法**（模块级 const 存译文会把语言冻在加载那一刻），所以
 * 这个写法是 brief 明确要求的。代价是那条 key **不在 `t(...)` 里**，LITERAL_CALL
 * 看不见它——拼错了同样一路进产物、运行时静默渲染成 key 原文。本判据补上这一段。
 *
 * 属性名必须以 `Key` 结尾且**不是**光秃秃的 `key`：Vue 的 `:key` / 路由名之类
 * 存的也是点分字符串（`key: 'nav.dashboard'`），收进来会误报。同理值必须是
 * 「点分小写标识符」形状——`sortKey: 'name'`（无点）天然被排除。
 */
const KEY_CONST = /\b[A-Za-z_$][\w$]*Key\s*:\s*(['"])([A-Za-z][\w$]*(?:\.[\w$]+)+)\1/g

/**
 * `data-section` 的属性值 —— 分节锚点。
 *
 * 迁移定稿后这个值是**词条 key**（`data-section="proxies.section.clash"`），
 * HlSectionRail 读到后 `t()` 显示。写错 / 漏建词条时，悬停气泡与 aria-label
 * 会直接渲染成 key 原文，而这条路径**既不在 `t(...)` 里、也不含中文**——
 * CJK 规则与上面两条判据都看不见它，只有这里能兜住。
 *
 * 只收「点分标识符形状」的值：迁移期尚未改的 legacy 值（`data-section="批量导入"`）
 * 天然不匹配，不会误报——那种情况由 CJK 规则负责报。
 * 只收静态属性：绑定写法 `:data-section="..."` 的表达式静态判不了，跳过。
 */
const DATA_SECTION = /\bdata-section\s*=\s*(['"])([A-Za-z][\w$]*(?:\.[\w$]+)+)\1/g

const missing = [] // 引用了不存在的 key
let literalCalls = 0
let dynamicCalls = 0
let keyConsts = 0
let sectionAnchors = 0

/* key → { sets: Set[]（各调用点所供参数名）, unknown: bool } */
const callSites = new Map()

for (const file of walk(SRC)) {
  if (file.startsWith(ZH_DIR) || file.startsWith(EN_DIR)) continue
  const rel = relative('.', file).split(sep).join('/')
  const src = stripComments(readFileSync(file, 'utf8'))
  for (const m of src.matchAll(LITERAL_CALL)) {
    const key = m[2]
    literalCalls += 1
    if (!zh.keys.has(key)) {
      const line = src.slice(0, m.index).split('\n').length
      missing.push({ file: rel, line, key, from: 't()' })
    }
  }
  for (const m of src.matchAll(CALL_ARGS)) {
    const key = m[2]
    let info = callSites.get(key)
    if (!info) {
      info = { sets: [], unknown: false }
      callSites.set(key, info)
    }
    if (m[3] === ')') {
      info.sets.push(new Set()) // 无参调用：供了空集
      continue
    }
    let i = m.index + m[0].length
    while (i < src.length && /\s/.test(src[i])) i += 1
    if (src[i] !== '{') {
      info.unknown = true // 参数是个变量，静态判不了
      continue
    }
    const names = readObjectKeys(src, i)
    if (names === null) info.unknown = true
    else info.sets.push(new Set(names))
  }
  for (const m of src.matchAll(KEY_CONST)) {
    keyConsts += 1
    const key = m[2]
    if (!zh.keys.has(key)) {
      const line = src.slice(0, m.index).split('\n').length
      missing.push({ file: rel, line, key, from: '常量表' })
    }
  }
  for (const m of src.matchAll(DATA_SECTION)) {
    sectionAnchors += 1
    const key = m[2]
    if (!zh.keys.has(key)) {
      const line = src.slice(0, m.index).split('\n').length
      missing.push({ file: rel, line, key, from: 'data-section' })
    }
  }
  dynamicCalls += [...src.matchAll(DYNAMIC_CALL)].length
}

/* ── 缺译 / 孤儿 ── */
const untranslated = [...zh.keys.keys()].filter((k) => !en.keys.has(k))
const orphans = [...en.keys.entries()]
  .filter(([k]) => !zh.keys.has(k))
  .map(([k, f]) => ({ key: k, file: f }))

/* ── 占位符对齐 ──
   词条值里的 `{name}` 必须在**每个调用点**都被供上：漏一个就少一个数
   （`共 {n} 个` 变 `共 个`），多写一个则原样渲染出 `{n}` 字面量——
   **两种都不报错**，而 ⑤⑥ 只看 key 在不在，看不见值里的占位符。

   判据是「值的占位符集合 ⊆ 调用点所供参数名集合」，取**两个语言的并集**
   作为「该 key 需要什么」，理由：一个 key 的两种语言可以合理地要不同的数据
   （`rates.chart.yAxis` 中文要 `{currency}` 显示「人民币」、英文要 `{code}`
   显示 "CNY"，调用点两个都传）。若改成判「中英占位符集合相等」，这条正确
   代码会被误报——而误报会让门禁失去威信，比漏报更糟。

   同一 key 有多个调用点时取**交集**语义：值只有一份（每种语言一份），
   任何一个调用点少传，那里就渲染出字面量。故任一处缺即报。
   调用点参数是变量 / 有展开运算时 info.unknown 置位，整条跳过（动态不判）。 */
const PLACEHOLDER = /\{(\w+)\}/g
const placeholders = (s) => new Set([...(s ?? '').matchAll(PLACEHOLDER)].map((m) => m[1]))

const placeholderMissing = []
for (const [key, info] of callSites) {
  if (info.unknown || !info.sets.length) continue
  const zhValue = zh.values.get(key)
  const enValue = en.values.get(key)
  if (zhValue === undefined && enValue === undefined) continue // 不存在的 key 由 ② 报
  const need = new Set([...placeholders(zhValue), ...placeholders(enValue)])
  if (!need.size) continue
  for (const supplied of info.sets) {
    const gaps = [...need].filter((n) => !supplied.has(n))
    if (gaps.length) {
      placeholderMissing.push({ key, gaps })
      break
    }
  }
}

/* ── en 词典的值里混进中文 ──
   逐条豁免：语言切换器的三个标签。它们**有意**用目标语言自己的写法——
   "Switch to 中文" 是给中文用户看的（告诉他点了会切到中文），
   写成 "Switch to Chinese" 反而错。除此之外英文词典的值里不该出现汉字。 */
const CJK = /[一-鿿]/
const EN_VALUE_EXEMPT = new Set(['shell.lang.toEn', 'shell.lang.toZh', 'shell.lang.targetZh'])
const enCjkValues = [...en.values.entries()]
  .filter(([k, v]) => typeof v === 'string' && CJK.test(v) && !EN_VALUE_EXEMPT.has(k))
  .map(([k, v]) => ({ key: k, value: v }))

/* ── 输出 ── */
const line = (s) => console.log(s)
line(`词典：zh-CN ${zh.keys.size} 条 / en ${en.keys.size} 条`)
line(
  `引用点：t() 字面量 ${literalCalls} 处 / 常量表 ${keyConsts} 处 / data-section 锚点 ${sectionAnchors} 处` +
    ` / 动态 ${dynamicCalls} 处（动态不判）`,
)

let failed = false

/* 两个词典都要查：en 侧的重复 key 会被 ⑤⑥ 的「key 齐不齐」判据掩盖——
   同名后写的静默覆盖先写的，而两边 key 集合照样相等，一切看起来正常。 */
const allDups = [
  ...zh.dups.map((d) => ({ ...d, dict: 'zh-CN' })),
  ...en.dups.map((d) => ({ ...d, dict: 'en' })),
]

if (allDups.length) {
  failed = true
  line(`\n✗ 重复 key ${allDups.length} 组（后写的会静默覆盖先写的）：`)
  for (const d of allDups) line(`   [${d.dict}] ${d.key}\n     ${d.a}\n     ${d.b}`)
}

if (missing.length) {
  failed = true
  line(`\n✗ 引用了不存在的 key ${missing.length} 处：`)
  for (const m of missing) line(`   ${m.file}:${m.line}  [${m.from}] ${m.key}`)
}

if (untranslated.length) {
  failed = true
  line(`\n✗ 缺英文翻译 ${untranslated.length} 条（英文界面下会回退成中文）：`)
  for (const k of untranslated) line(`   ${k}   [${zh.keys.get(k)}]`)
}

if (orphans.length) {
  failed = true
  line(`\n✗ 孤儿词条 ${orphans.length} 条（en 有、zh-CN 没有）：`)
  for (const o of orphans) line(`   ${o.key}   [${o.file}]`)
}

if (placeholderMissing.length) {
  failed = true
  line(`\n✗ 占位符没供上 ${placeholderMissing.length} 条（值里有 {name}，调用点没传 → 渲染成字面量）：`)
  for (const p of placeholderMissing) line(`   ${p.key}   缺：${p.gaps.join(', ')}`)
}

if (enCjkValues.length) {
  failed = true
  line(`\n✗ 英文词典的值里还是中文 ${enCjkValues.length} 条（key 对齐了，值是中文——英文界面下这条照旧显示中文）：`)
  for (const e of enCjkValues) line(`   ${e.key}   → ${e.value.slice(0, 40)}`)
}

if (!failed) {
  line('\n✓ 词典完整：无重复 key、无失效引用、中英逐条对齐、占位符一致、英文值无中文残留')
  process.exit(0)
}

if (reportOnly) {
  line('\n（--report 模式：只列不判，退出码 0）')
  process.exit(0)
}
process.exit(1)
