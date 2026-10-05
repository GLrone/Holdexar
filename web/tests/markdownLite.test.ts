import assert from 'node:assert/strict'
import { test } from 'node:test'

import { mdLiteToHtml } from '../src/lib/markdownLite.ts'

test('段落与换行', () => {
  assert.equal(mdLiteToHtml('第一行\n第二行'), '<p>第一行<br>第二行</p>')
})

test('无序列表：- / · / * 统一归并为 ul', () => {
  assert.equal(
    mdLiteToHtml('- 甲\n· 乙\n* 丙'),
    '<ul><li>甲</li><li>乙</li><li>丙</li></ul>',
  )
})

test('有序列表与无序列表分段', () => {
  assert.equal(
    mdLiteToHtml('1. 先查\n2. 再比\n\n- 结论'),
    '<ol><li>先查</li><li>再比</li></ol><ul><li>结论</li></ul>',
  )
})

test('行内粗体、高亮与行内码（码优先、不嵌套）', () => {
  assert.equal(
    mdLiteToHtml('现价 **更低**，==史低==，代码 `a**b`'),
    '<p>现价 <strong>更低</strong>，<mark>史低</mark>，代码 <code>a**b</code></p>',
  )
})

test('小节标题：## 与 ### 同归 h3，标题内可行内标记', () => {
  assert.equal(mdLiteToHtml('## 现价结论'), '<h3>现价结论</h3>')
  assert.equal(mdLiteToHtml('### **结论**：==值得买=='), '<h3><strong>结论</strong>：<mark>值得买</mark></h3>')
})

test('提示框：连续 > 行合并为一个 blockquote', () => {
  assert.equal(
    mdLiteToHtml('> 提示：史低\n> 以国区为准'),
    '<blockquote><p>提示：史低<br>以国区为准</p></blockquote>',
  )
})

test('分隔线：--- / *** 产出 hr，不吞列表', () => {
  assert.equal(
    mdLiteToHtml('- 甲\n---\n- 乙'),
    '<ul><li>甲</li></ul><hr><ul><li>乙</li></ul>',
  )
  assert.equal(mdLiteToHtml('***'), '<hr>')
})

test('标题与提示框内容全转义', () => {
  assert.equal(mdLiteToHtml('## <b>标题</b>'), '<h3>&lt;b&gt;标题&lt;/b&gt;</h3>')
  assert.equal(mdLiteToHtml('> ==<i>重点</i>=='), '<blockquote><p><mark>&lt;i&gt;重点&lt;/i&gt;</mark></p></blockquote>')
})

test('HTML 全转义：script 与标签原样显示', () => {
  assert.equal(
    mdLiteToHtml('<script>alert(1)</script> & "x"'),
    '<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;</p>',
  )
})

test('围栏代码块：内容原样转义、不解析行内标记', () => {
  assert.equal(
    mdLiteToHtml('看这个：\n```\n**不是粗体** <b>\n```'),
    '<p>看这个：</p><pre><code>**不是粗体** &lt;b&gt;</code></pre>',
  )
})

test('未闭合围栏：剩余内容按代码块收尾', () => {
  assert.equal(mdLiteToHtml('```\nabc'), '<pre><code>abc</code></pre>')
})

test('空输入与纯空白', () => {
  assert.equal(mdLiteToHtml(''), '')
  assert.equal(mdLiteToHtml('\n \n'), '')
})

test('GFM 表格解析：表头、对齐与单元格行内样式', () => {
  const md = `| 游戏 | 现价 | 状态 |
| :--- | :---: | ---: |
| **博德之门3** | ¥208.6 | 平史低 |
| God of War | ¥138 | 平史低 |`
  const html = mdLiteToHtml(md)
  assert.ok(html.includes('<div class="pilot-table-wrap"><table class="pilot-table">'))
  assert.ok(html.includes('<th style="text-align: left">游戏</th>'))
  assert.ok(html.includes('<th style="text-align: center">现价</th>'))
  assert.ok(html.includes('<th style="text-align: right">状态</th>'))
  assert.ok(html.includes('<td style="text-align: left"><strong>博德之门3</strong></td>'))
  assert.ok(html.includes('<td style="text-align: right">平史低</td>'))
})
