<script setup lang="ts">
/**
 * 更新内容渲染：把清单里的说明文本按行解析成结构化块。
 *
 * 清单说明到前端时**已是剥掉 markdown 的纯文本**（后端 _plain_notes 去掉 #、**、
 * 链接与代码块并截断），所以这里不引 markdown 依赖，只做确定性分行解析：
 * 列表 / 引用 / 表格 / 小标题 / 段落。解析只负责呈现，不改写文字内容。
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    notes?: string
    /** 当前版本号：正文首行若只是「应用名 vX.Y.Z」版本标题则丢掉（报告头部已有版本号） */
    version?: string
  }>(),
  { notes: '', version: '' },
)

interface Block {
  kind: 'heading' | 'para' | 'quote' | 'list' | 'table'
  text?: string
  items?: string[]
  head?: string[]
  rows?: string[][]
}

/** 中日韩字符（含全角标点）：段落内折行合并时不插空格 */
const CJK = /[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]/
/** 只由「应用名 + 版本号」构成的一行 */
const TITLE_LINE = /^(?:holdexar\s*)?v?\d+\.\d+[\w.-]*$/i

function clean(line: string): string {
  return line.replace(/\*\*/g, '').replace(/`/g, '').trim()
}

/** 折行合并：两侧都是中日韩字符时直接相接，否则补一个空格 */
function joinLines(lines: string[]): string {
  return lines.reduce((acc, cur) => {
    if (!acc) return cur
    const prev = acc[acc.length - 1] ?? ''
    const next = cur[0] ?? ''
    return acc + (CJK.test(prev) && CJK.test(next) ? '' : ' ') + cur
  }, '')
}

/** 小标题判据：短、无句读、不以标点收尾（清单里的分节名都满足） */
function isHeading(line: string): boolean {
  if (line.length > 20) return false
  if (/[。；，]/.test(line)) return false
  return !/[.。:：;；,，]$/.test(line)
}

function splitCells(line: string): string[] {
  return line
    .replace(/^\||\|$/g, '')
    .split('|')
    .map((cell) => cell.trim())
}

const blocks = computed<Block[]>(() => {
  const lines = (props.notes || '').replace(/\r\n?/g, '\n').split('\n').map(clean)
  let i = 0
  while (i < lines.length && lines[i] === '') i += 1
  if (i < lines.length && TITLE_LINE.test(lines[i] ?? '')) i += 1

  const out: Block[] = []
  let para: string[] = []
  const flushPara = () => {
    if (para.length) {
      out.push({ kind: 'para', text: joinLines(para) })
      para = []
    }
  }

  while (i < lines.length) {
    const line = lines[i] ?? ''
    if (line === '') {
      flushPara()
      i += 1
      continue
    }

    if (line.startsWith('- ')) {
      flushPara()
      const items: string[] = []
      while (i < lines.length && (lines[i] ?? '').startsWith('- ')) {
        items.push((lines[i] ?? '').slice(2).trim())
        i += 1
      }
      out.push({ kind: 'list', items })
      continue
    }

    if (line.startsWith('> ')) {
      flushPara()
      const quote: string[] = []
      while (i < lines.length && (lines[i] ?? '').startsWith('> ')) {
        quote.push((lines[i] ?? '').slice(2).trim())
        i += 1
      }
      out.push({ kind: 'quote', text: joinLines(quote) })
      continue
    }

    if (line.startsWith('|')) {
      flushPara()
      const rows: string[][] = []
      while (i < lines.length && (lines[i] ?? '').startsWith('|')) {
        rows.push(splitCells(lines[i] ?? ''))
        i += 1
      }
      // 第二行是全 `---` 时它是分隔行，不当数据；否则第一行就是数据
      const separator = (rows[1] ?? []).every((cell) => /^:?-{2,}:?$/.test(cell))
      out.push({
        kind: 'table',
        head: separator ? (rows[0] ?? []) : [],
        rows: rows.slice(separator ? 2 : 0),
      })
      continue
    }

    if (isHeading(line)) {
      flushPara()
      out.push({ kind: 'heading', text: line })
      i += 1
      continue
    }

    para.push(line)
    i += 1
  }

  flushPara()
  return out
})
</script>

<template>
  <div v-if="blocks.length" class="upd-notes-r">
    <template v-for="(b, idx) in blocks" :key="idx">
      <h4 v-if="b.kind === 'heading'" class="upd-notes-r__h">{{ b.text }}</h4>
      <p v-else-if="b.kind === 'para'" class="upd-notes-r__p">{{ b.text }}</p>
      <blockquote v-else-if="b.kind === 'quote'" class="upd-notes-r__q">
        {{ b.text }}
      </blockquote>
      <ul v-else-if="b.kind === 'list'" class="upd-notes-r__ul">
        <li v-for="(item, j) in b.items ?? []" :key="j">{{ item }}</li>
      </ul>
      <table v-else class="upd-notes-r__table">
        <thead v-if="(b.head ?? []).length">
          <tr>
            <th v-for="(cell, j) in b.head ?? []" :key="j">{{ cell }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, j) in b.rows ?? []" :key="j">
            <td v-for="(cell, k) in row" :key="k">{{ cell }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.upd-notes-r {
  display: flex;
  flex-direction: column;
  gap: 9px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.upd-notes-r__h {
  margin: 0;
  font-size: 12.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--text-primary);
}

.upd-notes-r__p {
  margin: 0;
  word-break: break-word;
}

.upd-notes-r__q {
  margin: 0;
  padding: 7px 10px;
  border-left: 2px solid var(--accent-a40);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  background: var(--accent-a08);
  color: var(--text-secondary);
}

.upd-notes-r__ul {
  margin: 0;
  padding-left: 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.upd-notes-r__ul li {
  word-break: break-word;
}

.upd-notes-r__ul li::marker {
  color: var(--accent);
}

.upd-notes-r__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.upd-notes-r__table th,
.upd-notes-r__table td {
  padding: 5px 8px;
  border: 1px solid var(--border-soft);
  text-align: left;
  word-break: break-word;
}

.upd-notes-r__table th {
  background: var(--accent-a08);
  color: var(--text-primary);
  font-weight: 600;
}
</style>