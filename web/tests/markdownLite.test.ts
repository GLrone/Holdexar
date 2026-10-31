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

test('围栏代码块：内容原样转义、不解析行内标记，带复制钮', () => {
  const html = mdLiteToHtml('看这个：\n```\n**不是粗体** <b>\n```')
  assert.ok(html.includes('<p>看这个：</p>'), html)
  assert.ok(html.includes('<div class="pilot-code">'), html)
  assert.ok(html.includes('<pre><code>**不是粗体** &lt;b&gt;</code></pre>'), html)
  assert.ok(html.includes('data-action="copy-code"'), html)
  assert.ok(html.includes('aria-label="Copy"'), html)
})

test('未闭合围栏：剩余内容按代码块收尾', () => {
  const html = mdLiteToHtml('```\nabc')
  assert.ok(html.includes('<pre><code>abc</code></pre>'), html)
  assert.ok(html.includes('data-action="copy-code"'), html)
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

test('SVG 图块：白名单标签原样呈现并包容器', () => {
  const svg = '<svg viewBox="0 0 10 10"><rect x="1" y="1" width="8" height="8" fill="#E6F1FB" /><text x="2" y="5" font-size="4">链路</text></svg>'
  const html = mdLiteToHtml(`前置说明\n${svg}\n后置说明`)
  assert.ok(html.startsWith('<p>前置说明</p><div class="pilot-svg" role="region" aria-label="SVG">'))
  assert.ok(html.includes('<div class="pilot-svg-stage" role="img"><svg viewBox="0 0 10 10">'))
  assert.ok(html.includes('<rect x="1" y="1" width="8" height="8" fill="#E6F1FB" />'))
  assert.ok(html.includes('<text x="2" y="5" fontSize="4">链路</text></svg></div>'))
  assert.ok(html.includes('<div class="pilot-svg-source" style="display:none;"><pre><code>'))
  assert.ok(html.endsWith('<p>后置说明</p>'))
})

test('SVG 图块：脚本与事件属性被剥除', () => {
  const svg = '<svg><rect onload="alert(1)" x="1" width="2" height="2" /><script>alert(2)</script><foreignObject><body>hi</body></foreignObject></svg>'
  const html = mdLiteToHtml(svg)
  assert.ok(html.includes('<div class="pilot-svg"'))
  assert.ok(!html.includes('onload'))
  assert.ok(!html.includes('<script'))
  assert.ok(!html.includes('foreignObject'))
  assert.ok(!html.includes('alert'))
})

test('SVG 图块：属性值带 javascript 协议整体拒渲染（回落转义文本）', () => {
  const svg = '<svg><rect width="10" height="javascript:alert(1)" /></svg>'
  const html = mdLiteToHtml(svg)
  assert.ok(!html.includes('pilot-svg'))
  assert.ok(html.includes('&lt;svg&gt;'))
})

test('SVG 图块：未闭合（流式期）挂占位不闪现原始标记', () => {
  const html = mdLiteToHtml('<svg viewBox="0 0 2 2"><rect ', { svgPendingLabel: '图生成中' })
  assert.equal(html, '<div class="pilot-svg is-pending" role="status"><span>图生成中</span></div>')
})

test('SVG 图块：文本与属性值转义、实体还原后仍安全', () => {
  const svg = '<svg><text x="1" y="1">a&lt;b &amp; c</text><rect width="1" height="1" title="&quot;q&quot;" /></svg>'
  const html = mdLiteToHtml(svg)
  assert.ok(html.includes('a&lt;b &amp; c'))
  assert.ok(html.includes('title="&quot;q&quot;"'))
  assert.ok(!html.includes('<b '))
})

test('SVG 图块：支持 style 标签与行内 style 属性，并拦截危险 CSS', () => {
  const svg = '<svg viewBox="0 0 10 10"><style>.red { fill: #f00; }</style><rect class="red" width="5" height="5" style="stroke: #00f; stroke-width: 1px;" /></svg>'
  const html = mdLiteToHtml(svg)
  assert.ok(html.includes('<style>.red { fill: #f00; }</style>'))
  assert.ok(html.includes('style="stroke: #00f; stroke-width: 1px;"'))

  const badSvg = '<svg viewBox="0 0 10 10"><rect width="5" height="5" style="background: url(javascript:alert(1));" /></svg>'
  assert.ok(!mdLiteToHtml(badSvg).includes('pilot-svg'))
})

test('SVG 图块：支持 use、symbol、defs 与局部 fragment 引用', () => {
  const svg = '<svg viewBox="0 0 20 20"><defs><symbol id="dot"><circle r="2" /></symbol></defs><use href="#dot" x="5" y="5" fill="#f00" /></svg>'
  const html = mdLiteToHtml(svg)
  assert.ok(html.includes('<symbol id="dot">'))
  assert.ok(html.includes('<use href="#dot"'))

  const badUse = '<svg viewBox="0 0 20 20"><use href="javascript:alert(1)" /></svg>'
  assert.ok(!mdLiteToHtml(badUse).includes('pilot-svg'))
})

test('SVG 图块：支持 filter 滤镜图元与 clipPath', () => {
  const svg = '<svg viewBox="0 0 20 20"><defs><filter id="sh"><feDropShadow dx="1" dy="1" stdDeviation="2" /></filter><clipPath id="cp"><rect width="10" height="10" /></clipPath></defs><rect filter="url(#sh)" clip-path="url(#cp)" width="15" height="15" /></svg>'
  const html = mdLiteToHtml(svg)
  assert.ok(html.includes('<filter id="sh">'))
  assert.ok(html.includes('<feDropShadow'))
  assert.ok(html.includes('clip-path="url(#cp)"'))
})

test('SVG 图块：围栏包含 XML 声明、DOCTYPE 与注释正常提取渲染', () => {
  const fence = '```xml\n<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n<!-- 图表说明 -->\n<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" fill="#E6F1FB" /></svg>\n```'
  const html = mdLiteToHtml(fence)
  assert.ok(html.includes('<div class="pilot-svg" role="region" aria-label="SVG">'))
  assert.ok(html.includes('<circle cx="5" cy="5" r="4" fill="#E6F1FB" />'))
})

test('SVG 图块：缺少 viewBox 但具备数值宽高自动补全 viewBox 保持自适应', () => {
  const svg = '<svg width="200" height="100"><rect width="200" height="100" fill="#E6F1FB" /></svg>'
  const html = mdLiteToHtml(svg)
  assert.ok(html.includes('viewBox="0 0 200 100"'))
})

test('SVG 图块：流式未闭合状态下自动补全闭合标签并渐进式出图渲染', () => {
  const streamingPartial = '```svg\n<svg viewBox="0 0 100 100">\n<rect x="10" y="10" width="80" height="80" fill="#66c0f4" />\n<g stroke="#fff">\n<line x1="20" y1="50" x2="80" y2="50" />'
  const html = mdLiteToHtml(streamingPartial, { svgDrawingLabel: '绘制中...' })
  assert.ok(html.includes('class="pilot-svg is-drawing"'))
  assert.ok(html.includes('<span>绘制中...</span>'))
  assert.ok(html.includes('<rect x="10" y="10" width="80" height="80" fill="#66c0f4" />'))
  assert.ok(html.includes('<line x1="20" y1="50" x2="80" y2="50" /></g></svg></div>'))
})


test('SVG 图卡：工具栏带「添加到对话」与「复制代码」两个动作钮', () => {
  const svg = '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" /></svg>'
  const html = mdLiteToHtml(svg, { svgQuoteLabel: '添加到对话', svgCopyLabel: '复制代码' })
  assert.ok(html.includes('data-action="quote"'))
  assert.ok(html.includes('aria-label="添加到对话"'))
  assert.ok(html.includes('data-action="copy"'))
  assert.ok(html.includes('aria-label="复制代码"'))
})

test('SVG 图卡：缺省文案回落英文，不渲染空标签', () => {
  const svg = '<svg viewBox="0 0 10 10"><rect width="5" height="5" /></svg>'
  const html = mdLiteToHtml(svg)
  assert.ok(html.includes('data-action="quote"'))
  assert.ok(html.includes('aria-label="Quote"'))
})
