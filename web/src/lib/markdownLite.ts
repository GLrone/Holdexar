/**
 * 领航台回答的极简 Markdown 渲染：先整体 HTML 转义再拼标签，无注入面。
 * 块级：围栏代码块、##/### 小节标题、> 提示框、--- 分隔线、无序/有序列表（含 · 分点）、
 * SVG 图块（白名单消毒后原样呈现）、段落；
 * 行内：`行内码`、**粗体**、==高亮==。不解析链接与图片（收敛可点击面）。
 * 产出仅含 <pre><code><ul><ol><li><p><br><strong><h3><blockquote><hr><mark>
 * 与消毒后的 SVG 子树这些自拼标签，样式由组件侧 :deep() 提供。
 */

const ESCAPES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ESCAPES[c])
}

/** 行内解析在已转义文本上进行：`code` 优先于 ==mark== 优先于 **bold**，成对为止不嵌套。 */
function inline(escaped: string): string {
  return escaped
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/==([^=]+)==/g, '<mark>$1</mark>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
}

const UL_RE = /^\s*(?:[-*·•])\s+(.*)$/
const OL_RE = /^\s*(\d+)[.、)]\s+(.*)$/
const H_RE = /^\s*#{1,3}\s+(.+)$/
const QUOTE_RE = /^\s*>\s?(.*)$/
const HR_RE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/
const TABLE_DELIM_RE = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$/

function parseTableRow(line: string): string[] {
  let trimmed = line.trim()
  if (trimmed.startsWith('|')) trimmed = trimmed.slice(1)
  if (trimmed.endsWith('|')) trimmed = trimmed.slice(0, -1)
  return trimmed.split('|').map((c) => c.trim())
}

function parseTableAligns(line: string): Array<'left' | 'center' | 'right' | null> {
  return parseTableRow(line).map((cell) => {
    const left = cell.startsWith(':')
    const right = cell.endsWith(':')
    if (left && right) return 'center'
    if (right) return 'right'
    if (left) return 'left'
    return null
  })
}

function renderTable(headers: string[], aligns: Array<'left' | 'center' | 'right' | null>, rows: string[][]): string {
  const ths = headers
    .map((h, idx) => {
      const align = aligns[idx] ? ` style="text-align: ${aligns[idx]}"` : ''
      return `<th${align}>${inline(escapeHtml(h))}</th>`
    })
    .join('')
  const trs = rows
    .map((r) => {
      const tds = r
        .map((c, idx) => {
          const align = aligns[idx] ? ` style="text-align: ${aligns[idx]}"` : ''
          return `<td${align}>${inline(escapeHtml(c))}</td>`
        })
        .join('')
      return `<tr>${tds}</tr>`
    })
    .join('')
  return `<div class="pilot-table-wrap"><table class="pilot-table"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table></div>`
}

export function mdLiteToHtml(src: string, opts?: MdLiteOpts): string {
  const lines = (src || '').replace(/\r\n?/g, '\n').split('\n')
  const out: string[] = []
  let para: string[] = []
  let quote: string[] = []
  let list: { kind: 'ul' | 'ol'; items: string[] } | null = null
  let code: string[] | null = null

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${inline(escapeHtml(para.join('\n'))).replace(/\n/g, '<br>')}</p>`)
      para = []
    }
  }
  const flushQuote = () => {
    if (quote.length) {
      out.push(`<blockquote><p>${inline(escapeHtml(quote.join('\n'))).replace(/\n/g, '<br>')}</p></blockquote>`)
      quote = []
    }
  }
  const flushList = () => {
    if (list) {
      out.push(
        `<${list.kind}>${list.items.map((it) => `<li>${inline(it)}</li>`).join('')}</${list.kind}>`,
      )
      list = null
    }
  }
  const flushBlocks = () => {
    flushPara()
    flushQuote()
    flushList()
  }

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i]!
    if (code !== null) {
      if (/^\s*```/.test(raw)) {
        const body = code.join('\n')
        // 围栏里整块就是一张图时按图渲染（模型几乎总给 SVG 套 ```xml/```svg 围栏）
        const svgDoc = extractSvgDoc(body)
        out.push(
          svgDoc
            ? renderSvg(svgDoc, opts, body)
            : `<pre><code>${escapeHtml(body)}</code></pre>`,
        )
        code = null
      } else {
        code.push(raw)
      }
      continue
    }
    if (/^\s*```/.test(raw)) {
      flushBlocks()
      code = []
      continue
    }
    // SVG 图块：块级提取（<svg …</svg>，可跨行），消毒后整体呈现；
    // 流式期未闭合的尾部挂占位，避免原始标记闪现
    {
      const open = SVG_OPEN_RE.exec(raw)
      if (open) {
        if (para.length && para.every((p) => /^\s*(?:<\?xml|<!DOCTYPE|<!--)[\s\S]*(?:\?>|>|-->)\s*$/i.test(p))) {
          para = []
        }
        flushBlocks()
        if (open.index > 0) para.push(raw.slice(0, open.index))
        const buf: string[] = [raw.slice(open.index)]
        const end = collectSvg(lines, i, buf)
        out.push(renderSvg(buf.join('\n'), opts))
        if (end.tail) para.push(end.tail)
        i = end.i
        continue
      }
    }
    if (HR_RE.test(raw)) {
      flushBlocks()
      out.push('<hr>')
      continue
    }
    if (!raw.trim()) {
      flushBlocks()
      continue
    }
    // 表格识别：当前行含竖线且下一行为分割线
    const nextLine = lines[i + 1]
    if (raw.includes('|') && nextLine && TABLE_DELIM_RE.test(nextLine)) {
      flushBlocks()
      const headers = parseTableRow(raw)
      const aligns = parseTableAligns(nextLine)
      i++ // 跳过分割线
      const rows: string[][] = []
      while (i + 1 < lines.length && lines[i + 1]!.includes('|') && lines[i + 1]!.trim()) {
        i++
        rows.push(parseTableRow(lines[i]!))
      }
      out.push(renderTable(headers, aligns, rows))
      continue
    }
    const h = H_RE.exec(raw)
    if (h) {
      flushBlocks()
      out.push(`<h3>${inline(escapeHtml(h[1]))}</h3>`)
      continue
    }
    const q = QUOTE_RE.exec(raw)
    if (q) {
      flushPara()
      flushList()
      quote.push(q[1])
      continue
    }
    const ul = UL_RE.exec(raw)
    const ol = ul ? null : OL_RE.exec(raw)
    if (ul || ol) {
      flushPara()
      flushQuote()
      const kind = ul ? 'ul' : 'ol'
      const text = ul ? ul[1] : ol![2]
      if (!list || list.kind !== kind) {
        flushList()
        list = { kind, items: [] }
      }
      list.items.push(escapeHtml(text))
      continue
    }
    flushQuote()
    flushList()
    para.push(raw)
  }
  if (code !== null) {
    // 围栏未闭合（流式途中）：若包含 SVG 片段，进行渐进式实时出图渲染
    const body = code.join('\n')
    const hasSvgStart = /<\?xml[\s\S]*?<svg[\s>]|<!--[\s\S]*?<svg[\s>]|^\s*<svg[\s>]/i.test(body)
    const svgDoc = hasSvgStart ? extractSvgDoc(body, true) : null
    out.push(
      svgDoc
        ? renderSvg(svgDoc, opts, body)
        : `<pre><code>${escapeHtml(body)}</code></pre>`,
    )
  }
  flushBlocks()
  return out.join('')
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
  if (/(?:javascript|vbscript|expression|behavior)\s*:/i.test(clean)) return null
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
