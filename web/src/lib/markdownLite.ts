/**
 * 领航台消息的 Markdown 渲染：markdown-it 解析（html:false 全转义）+ SVG 图块独立白名单消毒。
 * 用户消息、助手回答、历史回放与流式终态共用本入口；原生 HTML 一律转义，链接经协议白名单，
 * SVG 走 sanitizeSvg 独立信任边界（白名单消毒后图卡呈现）。样式由组件侧 :deep() 提供。
 */
import MarkdownIt from 'markdown-it'
import mark from 'markdown-it-mark'

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ESCAPES[c]!)
}

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
md.use(mark)

/* 紧凑输出：块级标签间不产生换行，与既有对话气泡的视觉契约一致；
   紧凑列表的段落 hidden 时不套 <p>（与默认渲染同判据） */
md.renderer.rules.paragraph_open = (tokens, idx) => (tokens[idx]!.hidden ? '' : '<p>')
md.renderer.rules.paragraph_close = (tokens, idx) => (tokens[idx]!.hidden ? '' : '</p>')
md.renderer.rules.blockquote_open = () => '<blockquote>'
md.renderer.rules.blockquote_close = () => '</blockquote>'
md.renderer.rules.bullet_list_open = () => '<ul>'
md.renderer.rules.bullet_list_close = () => '</ul>'
md.renderer.rules.ordered_list_open = () => '<ol>'
md.renderer.rules.ordered_list_close = () => '</ol>'
md.renderer.rules.list_item_open = () => '<li>'
md.renderer.rules.list_item_close = () => '</li>'
md.renderer.rules.hr = () => '<hr>'
md.renderer.rules.softbreak = () => '<br>'
md.renderer.rules.fence = (tokens, idx) => {
  const lang = tokens[idx]!.info.trim()
  const cls = lang ? ` class="language-${md.utils.escapeHtml(lang)}"` : ''
  const label = md.utils.escapeHtml(renderOpts?.codeCopyLabel || 'Copy')
  return (
    `<div class="pilot-code">` +
    `<button type="button" class="pilot-code-copy" data-action="copy-code" aria-label="${label}" title="${label}">` +
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M15 5H6a2 2 0 0 0-2 2v9"></path></svg>` +
    `</button>` +
    `<pre><code${cls}>${md.utils.escapeHtml(tokens[idx]!.content.replace(/\n$/, ''))}</code></pre>` +
    `</div>`
  )
}
/* h1-h3 归 h3（聊天气泡视觉契约），h4-h6 真实级别 */
const headingTagOf = (tag: string): string => {
  const level = Number(tag.slice(1))
  return level <= 3 ? 'h3' : `h${level}`
}
md.renderer.rules.heading_open = (tokens, idx) => `<${headingTagOf(tokens[idx]!.tag)}>`
md.renderer.rules.heading_close = (tokens, idx) => `</${headingTagOf(tokens[idx]!.tag)}>`

/* 表格：pilot-table 包裹与对齐样式契约 */
md.renderer.rules.table_open = () => '<div class="pilot-table-wrap"><table class="pilot-table">'
md.renderer.rules.table_close = () => '</table></div>'
md.renderer.rules.thead_open = () => '<thead>'
md.renderer.rules.thead_close = () => '</thead>'
md.renderer.rules.tbody_open = () => '<tbody>'
md.renderer.rules.tbody_close = () => '</tbody>'
md.renderer.rules.tr_open = () => '<tr>'
md.renderer.rules.tr_close = () => '</tr>'
type RenderRule = NonNullable<MarkdownIt['renderer']['rules']['th_open']>
const cellOpen = (tag: 'th' | 'td'): RenderRule => (tokens, idx) => {
  const style = tokens[idx]!.attrGet('style')
  if (!style) return `<${tag}>`
  return `<${tag} style="text-align: ${style.replace(/^text-align:/, '').trim()}">`
}
md.renderer.rules.th_open = cellOpen('th')
md.renderer.rules.th_close = () => '</th>'
md.renderer.rules.td_open = cellOpen('td')
md.renderer.rules.td_close = () => '</td>'

/* 任务列表：列表项首段 [ ]/[x] 前缀换成纯展示 checkbox */
md.core.ruler.after('inline', 'pilot_task_list', (state) => {
  const tokens = state.tokens
  for (let i = 0; i < tokens.length; i++) {
    const inlineTok = tokens[i]
    if (!inlineTok || inlineTok.type !== 'inline') continue
    if (tokens[i - 1]?.type !== 'paragraph_open' || tokens[i - 2]?.type !== 'list_item_open') continue
    const first = inlineTok.children?.[0]
    if (!first || first.type !== 'text') continue
    const m = /^\[([ xX])\]\s+/.exec(first.content)
    if (!m) continue
    const box = new state.Token('html_inline', '', 0)
    box.content = `<input type="checkbox" disabled${m[1] === ' ' ? '' : ' checked'}>`
    first.content = first.content.slice(m[0].length)
    inlineTok.children!.unshift(box)
  }
})

/* 预处理：围栏外的自有语法归一（·/•/* 分点统一为 - 防止换弹头断列，
   「1、」中文序号；均要求后随空白，与旧行为同界） */
function normalizeLine(line: string): string {
  return line
    .replace(/^(\s*)[*·•]\s+/, '$1- ')
    .replace(/^(\s*)(\d+)、\s+/, '$1$2. ')
}

function isFenceOpen(line: string): RegExpExecArray | null {
  return /^\s*(`{3,}|~{3,})/.exec(line)
}

function fenceCloseRe(ch: string, len: number): RegExp {
  return new RegExp('^\\s*' + ch + '{' + len + ',}')
}

/* GFM 表格行内代码中的裸竖线按规范会切列；本产品契约要求码内竖线保字面，
   故对表格各行的码段内裸竖线补 \| 转义（已是 \| 的不动）。 */
const TABLE_DELIM_RE = /^\s*\|?\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)+\|?\s*$|^\s*\|?\s*:?-{1,}:?\s*\|?\s*$/

function tableCellCount(line: string): number {
  let t = line.trim()
  if (t.startsWith('|')) t = t.slice(1)
  if (t.endsWith('|')) t = t.slice(0, -1)
  if (!t) return 0
  return t.split(/(?<!\\)\|/).length
}

function protectCodeSpans(line: string): string {
  return line.replace(/(`+)[^`]*?\1/g, (m) => (m.includes('|') ? m.replace(/(?<!\\)\|/g, '\\|') : m))
}

function protectTableCodePipes(src: string): string {
  const lines = src.split('\n')
  let fence: { ch: string; len: number } | null = null
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!
    if (fence) {
      if (fenceCloseRe(fence.ch, fence.len).test(line)) fence = null
      continue
    }
    const open = isFenceOpen(line)
    if (open) {
      const marker = open[1]!
      fence = { ch: marker[0]!, len: marker.length }
      continue
    }
    const next = lines[i + 1]
    if (!line.includes('|') || !next || !TABLE_DELIM_RE.test(next)) continue
    if (tableCellCount(line) !== tableCellCount(next)) continue
    lines[i] = protectCodeSpans(line)
    for (let j = i + 2; j < lines.length; j++) {
      const row = lines[j]!
      if (!row.trim() || !row.includes('|')) break
      lines[j] = protectCodeSpans(row)
    }
  }
  return lines.join('\n')
}

/* SVG 前置分离器：把 SVG 块从源文摘出换成占位行，markdown-it 只渲染剩余纯 Markdown；
   渲染后按占位回填图卡，保持「Markdown 解析」与「SVG 消毒」两条信任边界互不污染 */
const PUA_OPEN = '\uE000'
const PUA_CLOSE = '\uE001'
const SVG_HEAD_LINE_RE = /^\s*(?:<\?[\s\S]*?\?>|<!DOCTYPE[\s\S]*?>|<!--[\s\S]*?-->)\s*$/i

function prepareSource(src: string, opts?: MdLiteOpts): { text: string; cards: string[] } {
  const lines = src.split('\n')
  const out: string[] = []
  const cards: string[] = []
  let fence: { ch: string; len: number } | null = null
  let fenceBuf: string[] = []
  let fenceOpenLine = ''

  const emitFence = (body: string, closed: boolean): boolean => {
    const hasSvgStart = /<\?xml[\s\S]*?<svg[\s>]|<!--[\s\S]*?<svg[\s>]|^\s*<svg[\s>]/i.test(body)
    const svgDoc = extractSvgDoc(body) ?? (closed || !hasSvgStart ? null : extractSvgDoc(body, true))
    if (!svgDoc) return false
    out.push(`${PUA_OPEN}${cards.length}${PUA_CLOSE}`)
    cards.push(renderSvg(svgDoc, opts, body))
    return true
  }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i]!
    if (fence) {
      if (fenceCloseRe(fence.ch, fence.len).test(raw)) {
        if (!emitFence(fenceBuf.join('\n'), true)) out.push(fenceOpenLine, ...fenceBuf, raw)
        fence = null
        fenceBuf = []
      } else {
        fenceBuf.push(raw)
      }
      continue
    }
    const open = isFenceOpen(raw)
    if (open) {
      const marker = open[1]!
      fence = { ch: marker[0]!, len: marker.length }
      fenceBuf = []
      fenceOpenLine = raw
      continue
    }
    const svgAt = SVG_OPEN_RE.exec(raw)
    if (svgAt) {
      // 图前紧邻的 XML 头/DOCTYPE/注释行按既有行为吞掉，不进正文
      while (out.length && SVG_HEAD_LINE_RE.test(out[out.length - 1]!)) out.pop()
      if (out.length && out[out.length - 1] !== '') out.push('')
      if (svgAt.index > 0) out.push(raw.slice(0, svgAt.index))
      const buf: string[] = [raw.slice(svgAt.index)]
      const end = collectSvg(lines, i, buf)
      out.push(`${PUA_OPEN}${cards.length}${PUA_CLOSE}`)
      cards.push(renderSvg(buf.join('\n'), opts))
      if (end.tail) out.push('', end.tail)
      else out.push('')
      i = end.i
      continue
    }
    out.push(normalizeLine(raw))
  }
  if (fence) {
    if (!emitFence(fenceBuf.join('\n'), false)) out.push(fenceOpenLine, ...fenceBuf)
  }
  return { text: out.join('\n'), cards }
}

/* 渲染期标签文案（复制钮等）由入口按次传入；md.render 为同步执行，取值后即清 */
let renderOpts: MdLiteOpts | undefined

export function mdLiteToHtml(src: string, opts?: MdLiteOpts): string {
  renderOpts = opts
  try {
    const prepared = prepareSource((src || '').replace(/\r\n?/g, '\n'), opts)
    const html = md.render(protectTableCodePipes(prepared.text))
    return html
      .replace(/<p>\uE000(\d+)\uE001<\/p>/g, (_, n) => prepared.cards[Number(n)] ?? '')
      .replace(/\uE000(\d+)\uE001/g, (_, n) => prepared.cards[Number(n)] ?? '')
  } finally {
    renderOpts = undefined
  }
}

/** 行内级渲染：带引用胶囊的消息按片段行内渲染（粗斜体/行内码/链接同契约），块级结构让位胶囊流 */
export function mdLiteInlineToHtml(src: string): string {
  return md.renderInline(src || '')
}

/* ───────────────────────── SVG 图块（白名单消毒与交互式展示） ─────────────────────────
 * 模型输出的调用链/图表/流程图以 <svg> 块进入回答。渲染走「提取 → 字符串级
 * 白名单消毒 → 交互图卡呈现」三步：标签与属性双白名单，其余危险项（脚本、事件
 * 属性、外链 href、foreignObject、外部样式表）一律剥除；文本与属性值安全转义。
 * 产出包含预览/源码切换与一键复制代码能力，纯字符串实现，无 DOM 依赖。 */

export interface MdLiteOpts {
  svgPendingLabel?: string
  svgPreviewLabel?: string
  svgCodeLabel?: string
  svgCopyLabel?: string
  /** SVG 图卡「加到对话」按钮文案（引用整块图/代码到输入框） */
  svgQuoteLabel?: string
  svgDrawingLabel?: string
  /** 围栏代码块复制按钮文案 */
  codeCopyLabel?: string
}

const SVG_OPEN_RE = /<svg[\s>]/i
const SVG_MAX_CHARS = 131072
const SVG_MAX_DEPTH = 32

const SVG_TAGS = new Set([
  // 根与容器
  'svg', 'g', 'defs', 'symbol', 'use',
  // 基本几何图形与路径
  'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
  // 文本与排版
  'text', 'tspan', 'textpath',
  // 样式、渐变与图案
  'style', 'lineargradient', 'radialgradient', 'stop', 'pattern',
  // 裁剪、遮罩与标记
  'clippath', 'mask', 'marker',
  // 元数据说明
  'title', 'desc',
  // 滤镜效果（基础滤镜图元）
  'filter', 'fedropshadow', 'fegaussianblur', 'feoffset', 'femerge', 'femergenode',
  'feblend', 'fecolormatrix', 'fecomposite', 'feflood', 'fecomponenttransfer',
  'fefuncr', 'fefuncg', 'fefuncb', 'fefunca',
])

const SVG_TAG_CANONICAL: Record<string, string> = {
  svg: 'svg',
  lineargradient: 'linearGradient',
  radialgradient: 'radialGradient',
  clippath: 'clipPath',
  textpath: 'textPath',
  fedropshadow: 'feDropShadow',
  fegaussianblur: 'feGaussianBlur',
  feoffset: 'feOffset',
  femerge: 'feMerge',
  femergenode: 'feMergeNode',
  feblend: 'feBlend',
  fecolormatrix: 'feColorMatrix',
  fecomposite: 'feComposite',
  feflood: 'feFlood',
  fecomponenttransfer: 'feComponentTransfer',
  fefuncr: 'feFuncR',
  fefuncg: 'feFuncG',
  fefuncb: 'feFuncB',
  fefunca: 'feFuncA',
}

const SVG_ATTRS = new Set([
  'id', 'class', 'style', 'viewbox', 'preserveaspectratio', 'width', 'height', 'role', 'title',
  'd', 'x', 'y', 'x1', 'x2', 'y1', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'points', 'dx', 'dy',
  'fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width', 'stroke-opacity',
  'stroke-dasharray', 'stroke-dashoffset', 'stroke-linecap', 'stroke-linejoin',
  'stroke-miterlimit', 'vector-effect', 'paint-order', 'opacity',
  'transform', 'transform-origin', 'gradientunits', 'gradienttransform', 'spreadmethod',
  'offset', 'stop-color', 'stop-opacity', 'fx', 'fy', 'fr',
  'markerwidth', 'markerheight', 'refx', 'refy', 'orient',
  'marker-start', 'marker-mid', 'marker-end',
  'clip-path', 'clip-rule', 'clippathunits', 'mask', 'maskunits', 'maskcontentunits', 'filter',
  'font-size', 'font-weight', 'font-family', 'font-style', 'font-variant',
  'text-anchor', 'dominant-baseline', 'alignment-baseline', 'baseline-shift',
  'letter-spacing', 'word-spacing', 'text-decoration', 'writing-mode', 'white-space',
  'href', 'xlink:href',
  'patternunits', 'patterncontentunits', 'patterntransform',
  'color', 'display', 'visibility', 'overflow', 'cursor', 'pointer-events',
  'shape-rendering', 'text-rendering', 'image-rendering',
  'in', 'in2', 'result', 'stddeviation', 'mode', 'type', 'values',
  'flood-color', 'flood-opacity', 'operator', 'k1', 'k2', 'k3', 'k4',
  'scale', 'xchannelselector', 'ychannelselector',
  'slope', 'intercept', 'amplitude', 'exponent', 'tablevalues',
  'startoffset', 'method', 'spacing', 'pathlength',
  'xmlns', 'xmlns:xlink', 'version',
])

/** 小写白名单键 → 规范驼峰拼法（SVG 属性大小写敏感，注入层按规范格式重组）。 */
const SVG_ATTR_CANONICAL: Record<string, string> = {
  viewbox: 'viewBox',
  preserveaspectratio: 'preserveAspectRatio',
  gradientunits: 'gradientUnits',
  gradienttransform: 'gradientTransform',
  spreadmethod: 'spreadMethod',
  markerwidth: 'markerWidth',
  markerheight: 'markerHeight',
  refx: 'refX',
  refy: 'refY',
  'clip-path': 'clip-path',
  'clip-rule': 'clipRule',
  clippathunits: 'clipPathUnits',
  maskunits: 'maskUnits',
  maskcontentunits: 'maskContentUnits',
  patternunits: 'patternUnits',
  patterncontentunits: 'patternContentUnits',
  patterntransform: 'patternTransform',
  'font-size': 'fontSize',
  'font-weight': 'fontWeight',
  'font-family': 'fontFamily',
  'font-style': 'fontStyle',
  'text-anchor': 'textAnchor',
  'dominant-baseline': 'dominantBaseline',
  'alignment-baseline': 'alignmentBaseline',
  'baseline-shift': 'baselineShift',
  'stop-color': 'stopColor',
  'stop-opacity': 'stopOpacity',
  'fill-opacity': 'fillOpacity',
  'fill-rule': 'fillRule',
  'stroke-width': 'strokeWidth',
  'stroke-opacity': 'strokeOpacity',
  'stroke-dasharray': 'strokeDasharray',
  'stroke-dashoffset': 'strokeDashoffset',
  'stroke-linecap': 'strokeLinecap',
  'stroke-linejoin': 'strokeLinejoin',
  'stroke-miterlimit': 'strokeMiterlimit',
  'vector-effect': 'vectorEffect',
  'paint-order': 'paintOrder',
  'marker-start': 'markerStart',
  'marker-mid': 'markerMid',
  'marker-end': 'markerEnd',
  stddeviation: 'stdDeviation',
  'flood-color': 'floodColor',
  'flood-opacity': 'floodOpacity',
  xchannelselector: 'xChannelSelector',
  ychannelselector: 'yChannelSelector',
  startoffset: 'startOffset',
  tablevalues: 'tableValues',
  pathlength: 'pathLength',
  'transform-origin': 'transformOrigin',
}

const SVG_TAG_RE = /<(\/?)([a-zA-Z][\w:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g
const SVG_ATTR_RE = /([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))/g
const SVG_VALUE_FORBID_RE = /(?:javascript|vbscript|data)\s*:/i

/** 从代码围栏中提取整篇 SVG 文档（允许包含 XML 头、DOCTYPE 与注释）。 */
function extractSvgDoc(body: string, allowIncomplete = false): string | null {
  const m = /<svg[\s>][\s\S]*<\/svg\s*>/i.exec(body)
  if (m) {
    const head = body.slice(0, m.index).trim()
    const tail = body.slice(m.index + m[0].length).trim()
    const cleanHead = head
      .replace(/<\?[\s\S]*?\?>/g, '')
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/<!DOCTYPE[\s\S]*?>/gi, '')
      .trim()
    const cleanTail = tail
      .replace(/<!--[\s\S]*?-->/g, '')
      .trim()
    if (cleanHead.length === 0 && cleanTail.length === 0) {
      return m[0]
    }
  }

  if (allowIncomplete) {
    const startMatch = /<svg[\s>]/i.exec(body)
    if (!startMatch) return null
    const head = body.slice(0, startMatch.index).trim()
    const cleanHead = head
      .replace(/<\?[\s\S]*?\?>/g, '')
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/<!DOCTYPE[\s\S]*?>/gi, '')
      .trim()
    if (cleanHead.length === 0) {
      return body.slice(startMatch.index)
    }
  }

  return null
}

/** 消毒 CSS 文本（支持 <style> 块与 style 属性值）。 */
function sanitizeCss(css: string): string | null {
  const clean = css
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/<!\[CDATA\[/gi, '')
    .replace(/\]\]>/g, '')
    .trim()
  if (!clean) return ''
  if (/(?:javascript|vbscript|expression|behavior)\s*(?::|\()/i.test(clean)) return null
  if (/@import/i.test(clean)) return null
  if (/-moz-binding/i.test(clean)) return null
  if (/<\/?(?:script|style)/i.test(clean)) return null
  const urlMatches = clean.matchAll(/url\s*\(([^)]*)\)/gi)
  for (const match of urlMatches) {
    const rawTarget = match[1]?.trim().replace(/^['"]|['"]$/g, '') ?? ''
    if (!/^#[\w.:-]+$/.test(rawTarget)) return null
  }
  return clean
}

/** 从第 i 行起收集到 </svg> 为止；返回结束行下标与闭合标签后的行内残余。 */
function collectSvg(lines: string[], start: number, buf: string[]): { i: number; tail: string } {
  const closeEnd = (line: string): number => {
    const m = /<\/svg\s*>/i.exec(line)
    return m ? m.index + m[0].length : -1
  }
  const first = buf[0] ?? ''
  const e0 = closeEnd(first)
  if (e0 >= 0) {
    buf[0] = first.slice(0, e0)
    return { i: start, tail: first.slice(e0) }
  }
  for (let i = start + 1; i < lines.length; i++) {
    const line = lines[i]!
    const e = closeEnd(line)
    if (e >= 0) {
      buf.push(line.slice(0, e))
      return { i, tail: line.slice(e) }
    }
    buf.push(line)
  }
  return { i: lines.length - 1, tail: '' }
}

/** 白名单消毒一段 SVG 源码；结构非法或超限返回 null（调用方回落代码块）。
 * @param allowIncomplete 为 true 时允许未闭合的流式半截 SVG，自动补全闭合标签以供实时渐进式渲染。
 */
export function sanitizeSvg(src: string, allowIncomplete = false): string | null {
  const source = src
    .trim()
    .replace(/<!--[\s\S]*?-->/g, (m) => ' '.repeat(m.length))
    .replace(/<\?[\s\S]*?\?>/g, (m) => ' '.repeat(m.length))
    .replace(/<!DOCTYPE[\s\S]*?>/gi, (m) => ' '.repeat(m.length))
  if (!source || source.length > SVG_MAX_CHARS) return null
  if (!allowIncomplete && /<!|<\/svg[\s\S]*<\/svg/i.test(source)) return null
  let out = ''
  const stack: string[] = []
  let cursor = 0
  let skipping: string | null = null
  let skipDepth = 0
  let elementCount = 0

  for (const m of source.matchAll(SVG_TAG_RE)) {
    const [full, slash, rawName, rawAttrs] = m
    const text = source.slice(cursor, m.index)
    cursor = m.index + full.length
    if (text && skipping === null) out += escapeHtml(decodeEntities(text))
    const name = rawName.toLowerCase()
    const selfClosed = full.trimEnd().endsWith('/>')

    if (skipping !== null) {
      if (slash) {
        if (name === skipping && --skipDepth <= 0) skipping = null
      } else if (!selfClosed && name === skipping) {
        skipDepth++
      }
      continue
    }

    if (slash) {
      const open = stack.pop()
      if (open === name) {
        const canonicalTag = SVG_TAG_CANONICAL[name] ?? name
        out += `</${canonicalTag}>`
      }
      continue
    }

    if (!SVG_TAGS.has(name)) {
      if (!selfClosed) {
        skipping = name
        skipDepth = 1
      }
      continue
    }

    if (stack.length >= SVG_MAX_DEPTH) return null

    // 针对 <style> 标签特殊处理内部 CSS 文本并安全放行
    if (name === 'style') {
      const closeMatch = /<\/style\s*>/i.exec(source.slice(cursor))
      if (!closeMatch) {
        if (!allowIncomplete) return null
        const rawCss = source.slice(cursor)
        cursor = source.length
        const safeCss = sanitizeCss(rawCss)
        if (safeCss !== null && safeCss.length > 0) {
          out += `<style>${safeCss}</style>`
          elementCount++
        }
        continue
      }
      const rawCss = source.slice(cursor, cursor + closeMatch.index)
      cursor += closeMatch.index + closeMatch[0].length
      const safeCss = sanitizeCss(rawCss)
      if (safeCss !== null) {
        out += `<style>${safeCss}</style>`
        elementCount++
      }
      continue
    }

    const attrRes = sanitizeSvgAttrs(rawAttrs ?? '', name)
    if (attrRes === null) return null
    let attrs = attrRes.attrsStr

    // 若 <svg> 根标签未给 viewBox 但具有显式数值宽高，自动合成 viewBox 保持自适应缩放比
    if (name === 'svg' && !attrRes.hasViewBox && attrRes.widthNum !== null && attrRes.heightNum !== null) {
      attrs += ` viewBox="0 0 ${attrRes.widthNum} ${attrRes.heightNum}"`
    }

    const canonicalTag = SVG_TAG_CANONICAL[name] ?? name
    out += `<${canonicalTag}${attrs}${selfClosed ? ' />' : '>'}`
    if (name !== 'svg') elementCount++
    if (!selfClosed) stack.push(name)
  }

  const rest = source.slice(cursor)
  if (rest && skipping === null) {
    const cleanRest = allowIncomplete ? rest.replace(/<[^>]*$/, '') : rest
    if (cleanRest) out += escapeHtml(decodeEntities(cleanRest))
  }

  if (allowIncomplete) {
    if (stack.length === 0 || stack[0] !== 'svg') return null
    if (elementCount === 0) return null
    for (let j = stack.length - 1; j >= 0; j--) {
      const tag = stack[j]!
      const canonicalTag = SVG_TAG_CANONICAL[tag] ?? tag
      out += `</${canonicalTag}>`
    }
    return out
  }

  if (skipping !== null || stack.length > 0) return null
  if (!/^<svg[\s>]/.test(out)) return null
  return out
}

function decodeEntities(s: string): string {
  return s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#0*39;|&apos;/g, "'")
    .replace(/&amp;/g, '&')
}

interface SvgAttrResult {
  attrsStr: string
  hasViewBox: boolean
  widthNum: number | null
  heightNum: number | null
}

function sanitizeSvgAttrs(raw: string, _tagName: string): SvgAttrResult | null {
  let out = ''
  let hasViewBox = false
  let widthNum: number | null = null
  let heightNum: number | null = null

  for (const m of raw.matchAll(SVG_ATTR_RE)) {
    const rawAttr = m[1]!
    const fullLower = rawAttr.toLowerCase()
    const strippedLower = fullLower.replace(/^[\w-]+:/, '')
    const isXlink = fullLower === 'xlink:href'
    const key = isXlink ? 'xlink:href' : (SVG_ATTRS.has(fullLower) ? fullLower : strippedLower)
    if (!SVG_ATTRS.has(key)) continue

    const value = m[2] ?? m[3] ?? m[4] ?? ''
    const decodedVal = decodeEntities(value).trim()

    // 杜绝危险脚本协议注入
    if (SVG_VALUE_FORBID_RE.test(decodedVal)) return null

    // 局部锚点引用安全约束
    if (key === 'href' || key === 'xlink:href') {
      if (!/^#[\w.:-]+$/.test(decodedVal)) return null
      out += ` ${key}="${escapeHtml(decodedVal)}"`
      continue
    }

    // 行内样式安全过滤
    if (key === 'style') {
      const safeStyle = sanitizeCss(decodedVal)
      if (safeStyle === null) return null
      out += ` style="${escapeHtml(safeStyle)}"`
      continue
    }

    if (key === 'viewbox') hasViewBox = true
    if (key === 'width') {
      const num = parseFloat(decodedVal)
      if (!Number.isNaN(num) && num > 0) widthNum = num
    }
    if (key === 'height') {
      const num = parseFloat(decodedVal)
      if (!Number.isNaN(num) && num > 0) heightNum = num
    }

    const canonicalKey = SVG_ATTR_CANONICAL[key] ?? key
    out += ` ${canonicalKey}="${escapeHtml(decodedVal)}"`
  }

  return { attrsStr: out, hasViewBox, widthNum, heightNum }
}

function renderSvg(src: string, opts?: MdLiteOpts, rawSource?: string): string {
  const complete = /<\/svg\s*>\s*$/i.test(src.trim())
  const sanitized = complete
    ? (sanitizeSvg(src) ?? sanitizeSvg(src, true))
    : sanitizeSvg(src, true)

  if (sanitized !== null) {
    const previewLabel = escapeHtml(opts?.svgPreviewLabel || 'Preview')
    const codeLabel = escapeHtml(opts?.svgCodeLabel || 'Code')
    const copyLabel = escapeHtml(opts?.svgCopyLabel || 'Copy')
    const quoteLabel = escapeHtml(opts?.svgQuoteLabel || 'Quote')
    const drawingLabel = escapeHtml(opts?.svgDrawingLabel || 'Drawing...')
    const rawCode = escapeHtml(rawSource ?? sanitized)
    const isDrawing = !complete
    return (
      `<div class="pilot-svg${isDrawing ? ' is-drawing' : ''}" role="region" aria-label="SVG">` +
      `<div class="pilot-svg-toolbar">` +
      `<span class="pilot-svg-badge${isDrawing ? ' is-drawing' : ''}">` +
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline></svg>` +
      `<span>${isDrawing ? drawingLabel : 'SVG'}</span>` +
      `</span>` +
      `<div class="pilot-svg-actions">` +
      `<button type="button" class="pilot-svg-tab is-active" data-action="preview">${previewLabel}</button>` +
      `<button type="button" class="pilot-svg-tab" data-action="code">${codeLabel}</button>` +
      `<button type="button" class="pilot-svg-quote" data-action="quote" title="${quoteLabel}" aria-label="${quoteLabel}">` +
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 7H5a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h2v1a3 3 0 0 1-3 3"></path><path d="M19 7h-4a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h2v1a3 3 0 0 1-3 3"></path></svg>` +
      `</button>` +
      `<button type="button" class="pilot-svg-copy" data-action="copy" title="${copyLabel}" aria-label="${copyLabel}">` +
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M15 5H6a2 2 0 0 0-2 2v9"></path></svg>` +
      `</button>` +
      `</div>` +
      `</div>` +
      `<div class="pilot-svg-stage" role="img">${sanitized}</div>` +
      `<div class="pilot-svg-source" style="display:none;"><pre><code>${rawCode}</code></pre></div>` +
      `</div>`
    )
  }
  if (!complete) {
    const label = opts?.svgPendingLabel ? escapeHtml(opts.svgPendingLabel) : ''
    return `<div class="pilot-svg is-pending" role="status">${label ? `<span>${label}</span>` : ''}</div>`
  }
  return `<pre><code>${escapeHtml(src)}</code></pre>`
}
