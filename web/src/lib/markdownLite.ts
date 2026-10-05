/**
 * 领航台回答的极简 Markdown 渲染：先整体 HTML 转义再拼标签，无注入面。
 * 块级：围栏代码块、##/### 小节标题、> 提示框、--- 分隔线、无序/有序列表（含 · 分点）、段落；
 * 行内：`行内码`、**粗体**、==高亮==。不解析链接与图片（收敛可点击面）。
 * 产出仅含 <pre><code><ul><ol><li><p><br><strong><h3><blockquote><hr><mark> 这些自拼标签，
 * 样式由组件侧 :deep() 提供。
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

export function mdLiteToHtml(src: string): string {
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
        out.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`)
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
  if (code !== null) out.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`)
  flushBlocks()
  return out.join('')
}
