#!/usr/bin/env node
/* 颜色令牌化门禁 —— 与 check-routes.mjs 同构，走 `npm run build` 链。
 *
 * 为什么需要它（ESLint 管不到）：颜色字面量有两处藏身处 ESLint 的 AST 看不见——
 *   ① `<style>` 块里的 CSS（AST 里没有 CSS 节点）；
 *   ② 模板的 `style="color:#8f98a0"` 行内属性。
 * 图表 option 里那份已由 holdexar/chart-option-contract 规则覆盖，这里补 CSS 层。
 *
 * 判据不是「有没有写 #hex」——那会连 token 契约文件本身一起报，等于没有判据。
 * 判据是**这个字面量是不是某个 token 的取值**：抄一份 token 的值当字面量，
 * 正是「主题一切换就再不跟随」的成因（#8f98a0 是深色 --text-muted 的值，
 * #a4d007 是深色 --success 的值，被抄下来后浅色主题下挂着深色的色）。
 * 因此：命中 token 取值 → 报错并指名该用哪个 token；
 *       不在任何 token 取值里 → 是刻意设计色（分类色板等），放行。
 *
 * 例外登记：`ALLOW` 清单按「文件 + 色值」登记并写明理由，与例外登记表一一对应。
 * 用法：node scripts/check-colors.mjs [--report]
 *       --report 只列不判（用于盘点），不带参数则违规即 exit 1。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'

const TOKENS_FILE = join('src', 'styles', 'tokens.css')

/** 颜色字面量：hex（3/4/6/8 位）与 rgb/rgba/hsl/hsla 函数式 */
const COLOR_RE =
  /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b|\b(?:rgba?|hsla?)\s*\([^)]*\)/g

/** 归一化：#FFF → #ffffff，rgba 去掉空白 */
function norm(s) {
  const t = s.toLowerCase().replace(/\s+/g, '')
  const short = t.match(/^#([0-9a-f])([0-9a-f])([0-9a-f])$/)
  return short ? `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}` : t
}

/**
 * token 取值 → 定义它的 token 名（一个取值可能被多个 token 共用，如 muted/dim）。
 *
 * 两类 token 不参与建表，否则会给出一看就不对的建议：
 *   ① `--shadow-*`：阴影是**复合值**，里面那个 rgba 是投影色，永远不会是某个
 *      `color:` / `background:` 该去取的令牌。不排除的话，`box-shadow: 0 15px 40px
 *      rgba(0,0,0,.5)` 会命中深色 `--shadow-dot` 里的同一个 rgba，报成「该用 var(--shadow-dot)」——
 *      而 --shadow-dot 是 `0 0 5px …` 的整条阴影，换上去等于把投影改成发光点。
 *   ② 含 `gradient(` 的复合 token：它的色标与 `--bg-base` / `--surface-card-2` 等
 *      基础 token 是同一批值，基础 token 已单独建表，这里再收一遍只会让建议列表变糊。
 */
function collectTokenValues(src) {
  const map = new Map()
  for (const m of src.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)) {
    const name = m[1]
    const val = m[2].trim()
    if (/^--shadow-/.test(name) || /gradient\(/i.test(val)) continue
    const found = val.match(COLOR_RE)
    if (!found) continue
    for (const c of found) {
      const k = norm(c)
      if (!map.has(k)) map.set(k, new Set())
      map.get(k).add(name)
    }
  }
  return map
}

/**
 * 例外登记 —— 仅登记**已确认无法令牌化**的处所，须写明理由。
 * 新加一条视同违反收口规则（与 eslint 的 LEGACY_FILES 同一纪律）。
 *
 * `colors` 必填：要么是色值数组（只豁免这几个值），要么 `'*'` 整体豁免。
 * 之所以不给「整文件豁免」当默认——那会让登记一处色值顺手放过整个文件，
 * 登记表就失去意义了（本文件自己的注释里写过这个教训）。
 */
const ALLOW = [
  {
    file: 'src/api/chartTheme.ts',
    colors: '*',
    reason:
      '图表取色层——它就是**把 token 翻译成 echarts 能吃的字面量**的那一层。' +
      "这里的字面量全都是 `cssVar('--x', '#fallback')` 里的回退值，作用是令牌缺席时不至于画不出图，" +
      '语义上等价于 CSS 的 `var(--x, 字面量)`（那条已在主循环里放行）。其余是浅/深两套主题的**定义**，' +
      '与 tokens.css 同性质。要求它「不写字面量」等于要求它不工作。',
  },
  {
    file: 'src/lib/familyColors.ts',
    colors: '*',
    reason:
      'family 分类色板的唯一出处。这里**必须**是 `*`：本文件通篇就是色板定义，' +
      '逐个色值登记等于把同一份色板抄两遍，改一处必漏另一处。' +
      '（其余条目的 `*` 都已按道理由「整文件」收窄到「色值清单」，见文件末的说明。）',
  },
  {
    file: 'src/components/ui/HlGooeyNav.vue',
    colors: [
      '#66c0f4', '#1a9fff', '#a4ce3d', '#60a820',
      '#ab57dd', '#782bb0', '#e5e4e2', '#8f98a0',
      '#ffffff',
    ],
    reason:
      'GooeyNav 的 **8 色粒子色板**（`COLOR_VARS`，写进行内 `--color` 供 .hl-gn-point 取用）：' +
      '粒子要的是「彼此可区分」而非语义，且刻意不随主题变。' +
      '（导航项本身的选中色不在这里——那是 CSS 的 `var(--accent)` + `var(--on-accent)`。）' +
      '收窄到色值清单而不再是整文件豁免：原先的 `*` 把这个文件里任何色值都放过了，' +
      "包括该走的令牌——登记表一旦能「登记一处、放过整文件」就失去意义了。" +
      "`#ffffff` 是 `COLOR_VARS[color] ?? '#fff'` 的回退值，与 `var(--x, 字面量)` 同性质。",
  },
  {
    file: 'src/components/ProductTour.vue',
    colors: ['#ffffff'],
    reason:
      'SVG `<mask>` 里 `fill="#fff"` 是**遮罩强度**语义（白=显示、黑=遮住），不是颜色——' +
      '同一个元素上的 `fill="#000"` / `fill-opacity="0.55"` 同理。换成 var(--text-on-fill) ' +
      '只是碰巧同值，语义完全错位，而且一旦哪个主题把该 token 调暗，聚光镂空就会跟着漏。',
  },
]

/*
 * 登记纪律（本表自己的规矩，写在表尾以便加条目时先看到）：
 *
 * ① `colors` 默认写**色值清单**，只有「整文件通篇就是色板定义」才配得上 `*`
 *    （目前仅 chartTheme.ts 与 lib/familyColors.ts 两处）。
 *    原先 HlGooeyNav.vue / hl-framework.css / tabs-shared.css 三条都是 `*`，
 *    与这条规矩自相矛盾——`*` 把文件里**该走令牌**的色值一并放过了。
 *
 * ② **没有字面量需要豁免的文件不登记**。`views/family/tabs/tabs-shared.css`
 *    原先在表内（理由写的是「family 分类色板的 CSS 侧登记处」），但该文件里
 *    一个命中 token 取值的字面量都没有：`--gh-0..4` 热力色阶在 `.dark` 作用域内
 *    （本就由主题作用域豁免），`.bill-type--gift` 的 #ec4899 不在任何 token 取值里
 *    （属「刻意设计色」，本就放行）。登记一个不需要豁免的文件，等于白送一份豁免。
 */

/**
 * 作用域豁免：以下选择器块内的字面量归**该层自己**管，不由本门禁判。
 *
 *   · 主题层（`.dark` / `:not(.dark)`）——「按主题写死」正是主题层的职责
 *     （`hl-light.css` 整个文件都是 `html:not(.dark)` 前缀，tokens.css
 *     的深色半区是 `html.dark`）。
 *   · 固定纹样层（`.hl-btn-art`）——整套「艺术按钮」的配色是一份自洽的固定设计：
 *     金 `#ffe066→#ffb347→#ff9a3c` 配 `#4a2c0a` 字、红 `#ff7b6b→#e74c3c` 配
 *     `#7a1f12` 边与 `#fff5f2` 字、combo 系的暗底加白波纹。换任何语义令牌都会
 *     拆掉它——而它里面 `#e74c3c` / `#ffffff` / `rgba(255,255,255,.05)` 恰好等于
 *     深色 `--danger` / `--text-on-fill` / `--line-1` 的取值，才被误判。
 *   · 固定 chrome 层（`.hl-carousel`）——圆点与箭头是 `position: absolute` 钉在
 *     **幻灯片之内**的，而幻灯片永远是有底的（调用方传 `colors[i]`，否则组件自己
 *     生成 `var(--accent-deep)→var(--accent)→var(--success)` 渐变）。所以白圆点、
 *     半透明黑底的箭头是「压在任意底图上」的恒定 chrome，两套主题下都对。
 *
 * 为什么这两层登记**作用域**而不是 ALLOW 色值清单：这里不能按色值放行。
 * `#ffffff` 在本文件别处（`.hl-gooey-nav li.is-active` 压在 `var(--accent)` 上）
 * 是**该走令牌**的——按色值登记会连它一起放过，那正是 ALLOW 表被收窄的原因。
 *
 * 只有**不受这些作用域管**的声明才必须走 token——那种声明在另一套主题下会挂着
 * 上一套主题的色值，这正是本门禁要拦的东西。
 *
 * ⚠️ 写**前缀**（`/\.hl-carousel/`），不要写 `/\.hl-carousel\b/`：BEM 的 `__` 与 `--`
 * 里 `_` 是 `\w`，`l` 与 `_` 之间**没有**词边界，`\b` 会让 `.hl-carousel__dot`
 * 整条漏掉——本规则第一次写出来时正是这样漏的（`--pattern` 能命中是因为 `-` 非 `\w`，
 * 于是只有 `--` 变体被豁免、`__` 变体没有，看起来像是「豁免了一半」）。
 */
const SCOPED_RES = [/\.dark\b|:not\(\.dark\)/, /\.hl-btn-art/, /\.hl-carousel/]

/**
 * 逐行跟踪 CSS 块嵌套，返回**每一行的事件表**，供按列判定某个字面量是否在豁免作用域内。
 *
 * 为什么不能像最初那样只回一个「本行是否在作用域内」的布尔：
 * 单行写法 `html.dark .x { color: #fff; }` 会在**同一趟**里先 push 再 pop，
 * 于是行末取 stack.some() 拿到的是**外层**的状态，本行的豁免被自己弹掉了。
 * 表现为「豁免了一半」：跨行写法（`{` 在行尾、`}` 单独一行）正常，单行写法永远不豁免——
 * `.hl-carousel__dot.is-on { … }` 就是这么漏的。改成按列取值后两种写法一致。
 *
 * 每个事件记 `exempt` = **处理完该事件之后** stack.some(Boolean)：
 * 用 some() 而非栈顶，因为 @media 之类的内层块自身选择器不含 .dark，
 * 但语义上仍处在外层主题块之内；子块同理仍是「在里面」。
 *
 * `enter` = 本行第 0 列之前的状态。上一行没消费完的选择器文本（`pending`）会带到本行，
 * 其中的花括号算作负列事件，因而对第 0 列起的字面量同样生效。
 */
function scopedSelectorMap(lines) {
  const map = []
  const stack = []
  let pending = ''
  lines.forEach((raw) => {
    const enter = stack.some(Boolean)
    const text = pending + raw.replace(/\/\*.*?\*\//g, '')
    const offset = pending.length // text[k] 与「本行列号」相差 offset
    pending = ''
    const events = []
    let idx = 0
    while (idx < text.length) {
      const open = text.indexOf('{', idx)
      const close = text.indexOf('}', idx)
      if (open === -1 && close === -1) break
      if (open !== -1 && (close === -1 || open < close)) {
        stack.push(SCOPED_RES.some((re) => re.test(text.slice(idx, open))))
        events.push({ col: open - offset, exempt: stack.some(Boolean) })
        idx = open + 1
      } else {
        stack.pop()
        events.push({ col: close - offset, exempt: stack.some(Boolean) })
        idx = close + 1
      }
    }
    pending = text.slice(idx)
    map.push({ enter, events })
  })
  return map
}

/** 第 col 列是否落在豁免作用域内（col 用 m.index，与行内列号同尺度） */
function inScopedBlock(span, col) {
  let state = span.enter
  for (const e of span.events) {
    if (e.col > col) break
    state = e.exempt
  }
  return state
}

/**
 * 「色值不算色值」的上下文消隐：把下列区段替换成**等长空格**，返回同长文本。
 *
 *   ① 注释（`/* *\/`、`<!-- -->`）——注释里举例写一句「原本写死成 #a4d007」
 *      不是在用它。为什么不能只判行首（`^//` / `^*` / `^<!--`）：那只挡得住
 *      单行注释，跨行注释中间那些行从行首看就是普通代码，本门禁自测时正是
 *      这样误报的（HlGameCard 一条多行注释的第 2 行）。
 *   ② 阴影取值（`box-shadow` / `text-shadow` 的整条值、`drop-shadow(...)`）——
 *      阴影里那个颜色是**投影色**，是「这东西投出的影子有多黑」，不是任何面的
 *      语义色。理由与 collectTokenValues 里排除 `--shadow-*` **同源**：
 *      `inset 0 1px 0 rgba(255,255,255,.1)` 会命中深色 `--row-border`、
 *      `0 2px 8px rgba(0,0,0,.2)` 会命中 `--surface-panel`，两条建议都是错的
 *      （换上去等于把投影改成一条描边）。
 *
 * 为什么阴影必须做到**跨行**：上面那条 `inset` 高光恰好写在跨行 box-shadow 的
 * 第 4 行上，只判「本行含 shadow」必漏。
 *
 * 留等长空格而不是整段删除，是为了保住列号——下面判 `var(--x, 字面量)` 回退
 * 时用的是 `m.index`。
 *
 * 已知不覆盖：JS 行尾 `//` 注释（要区分它和字符串里的 `https://` 得写词法器，
 * 代价与收益不成比例，故仍由主循环只判**行首**注释兜底）。
 */
function blankNonColorContexts(src) {
  const out = src.split('')
  const blankTo = (start, end) => {
    for (let k = start; k < end && k < out.length; k++) if (out[k] !== '\n' && out[k] !== '\r') out[k] = ' '
  }

  // ① 注释
  for (const [open, close] of [['/*', '*/'], ['<!--', '-->']]) {
    let from = 0
    for (;;) {
      const a = src.indexOf(open, from)
      if (a === -1) break
      const b = src.indexOf(close, a + open.length)
      const end = b === -1 ? src.length : b + close.length
      blankTo(a, end)
      from = end
    }
  }

  // ② 阴影取值。在「已消隐注释」的文本上做，这样注释里的分号不会被当成终止符。
  const masked = out.join('')
  const parenTo = (from) => {
    let depth = 0
    for (let k = from; k < masked.length; k++) {
      if (masked[k] === '(') depth++
      else if (masked[k] === ')') { depth--; if (!depth) return k + 1 }
    }
    return masked.length
  }
  for (const re of [/\b(?:box-shadow|text-shadow)\s*:/g, /\bdrop-shadow\s*\(/g]) {
    for (const m of masked.matchAll(re)) {
      const start = m.index + m[0].length
      const isFn = m[0].includes('(')
      let end
      if (isFn) end = parenTo(start - 1)
      else {
        end = masked.length
        let depth = 0
        for (let k = start; k < masked.length; k++) {
          const c = masked[k]
          if (c === '(') depth++
          else if (c === ')') depth--
          else if (!depth && (c === ';' || c === '}')) { end = k; break }
        }
      }
      blankTo(start, end)
    }
  }

  return out.join('')
}

/**
 * 收 `.ts` 一起扫：色值不止住在 CSS 与模板行内 style 里，`.ts` 里的常量色板同样
 * 会「抄一份 token 值再没跟随主题」。原先只收 `.vue`/`.css` 时，把
 * `familyColors.ts` 的色板删掉改放进某个 `.ts` 助手函数，就能整队绕过门禁。
 * 现全库 `.ts` 色彩字面量只有两处**登记在册的色板注册表**（见 ALLOW），
 * 扩到 `.ts` 不产生噪声，只是把「换个文件类型即可绕过」这个洞堵上。
 */
function walk(dir, out = []) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(vue|css|ts)$/.test(p)) out.push(p)
  }
  return out
}

/**
 * 一个取值常被十几个 token 共用（#ffffff 同时是 --bg-card / --surface-card /
 * --input-bg / --on-accent…），全列出来等于没说。按「语义直白程度」排个序，
 * 只留前三个：优先非 Element Plus 映射、非 alpha 阶梯、非 --color-hl-* 旧名，
 * 再按名字短的优先（短名通常是基础语义色）。
 */
function suggest(names) {
  const rank = (n) =>
    (/^--el-/.test(n) ? 8 : 0) + (/-a\d+$/.test(n) ? 4 : 0) + (/^--color-hl-/.test(n) ? 2 : 0)
  return [...names].sort((a, b) => rank(a) - rank(b) || a.length - b.length)
}

/** 该文件的这个色值是否已登记豁免（colors: '*' 为整体豁免） */
const isAllowed = (file, value) =>
  ALLOW.some(
    (a) => file.split(sep).join('/') === a.file && (a.colors === '*' || a.colors.includes(value)),
  )

const tokenSrc = readFileSync(TOKENS_FILE, 'utf8')
const valueToTokens = collectTokenValues(tokenSrc)
const tokensNorm = TOKENS_FILE.split(sep).join('/')

const violations = []
const foreign = []

for (const file of walk('src')) {
  const rel = file.split(sep).join('/')
  if (rel === tokensNorm) continue // token 契约本体：字面量的正当归属地
  const lines = blankNonColorContexts(readFileSync(file, 'utf8')).split(/\r?\n/)
  // 选择器作用域只对 CSS 有意义：<script>/<template> 区段里没有选择器概念，
  // 那里的字面量（行内 style、色板数组）一律按违规判，除非登记进 ALLOW。
  const scoped = /\.css$/.test(rel) ? scopedSelectorMap(lines) : []
  lines.forEach((line, i) => {
    const trimmed = line.trim()
    if (/^(\/\/|\*)/.test(trimmed)) return // 行首注释（块注释已由 blankNonColorContexts 消隐）
    for (const m of line.matchAll(COLOR_RE)) {
      // 豁免作用域内的字面量：主题适配层（html:not(.dark) 等）、固定纹样/chrome 层
      if (scoped.length && inScopedBlock(scoped[i], m.index)) continue
      // var(--x, <字面量>) 的 fallback 是正当写法：令牌缺席时才生效
      const before = line.slice(0, m.index)
      const open = before.lastIndexOf('var(')
      if (open !== -1 && !before.slice(open).includes(')') && before.slice(open).includes(',')) continue
      const names = [...(valueToTokens.get(norm(m[0])) ?? [])]
      if (!names.length) {
        foreign.push({ file: rel, line: i + 1, color: m[0] })
        continue
      }
      if (isAllowed(rel, norm(m[0]))) continue
      violations.push({ file: rel, line: i + 1, color: m[0], names })
    }
  })
}

// --json 输出全量（含 --report 截断掉的 foreign），供清剿时逐条对照
if (process.argv.includes('--json')) {
  console.log(JSON.stringify({ violations, foreign }, null, 1))
  process.exit(0)
}

const report = process.argv.includes('--report')
const grouped = new Map()
for (const v of violations) {
  if (!grouped.has(v.file)) grouped.set(v.file, [])
  grouped.get(v.file).push(v)
}

if (report) {
  console.log(`\n[盘点] 抄了 token 取值的字面量：${violations.length} 处 / ${grouped.size} 个文件`)
  for (const [file, vs] of [...grouped].sort((a, b) => b[1].length - a[1].length)) {
    console.log(`  ${String(vs.length).padStart(3)}  ${file}`)
    for (const v of vs) {
      console.log(
        `         :${v.line}  ${v.color}  →  ${suggest(v.names)
          .slice(0, 3)
          .map((n) => `var(${n})`)
          .join(' | ')}`,
      )
    }
  }
  console.log(`\n[盘点] 不在 token 取值内的其他字面量：${foreign.length} 处`)
  for (const f of foreign.slice(0, 60)) console.log(`  ${f.file}:${f.line}  ${f.color}`)
  if (foreign.length > 60) console.log(`  … 另 ${foreign.length - 60} 处`)
  process.exit(0)
}

if (violations.length) {
  console.error(
    `\n✗ 颜色令牌化门禁：${violations.length} 处字面量抄了 token 取值——写死即冻结在单主题，切主题不跟随。\n`,
  )
  for (const [file, vs] of grouped) {
    for (const v of vs) {
      console.error(`  ${file}:${v.line}  ${v.color}  →  请改用 ${v.names.map((n) => `var(${n})`).join(' 或 ')}`)
    }
  }
  console.error(
    '\n确属刻意设计色（分类色板等）请登记到本脚本的 ALLOW 清单并写明理由；' +
      '确属新语义请先在 tokens.css 里定义 token（浅深两套都要登记）。\n',
  )
  process.exit(1)
}

console.log('[check-colors] ✓ 无抄用 token 取值的颜色字面量')
