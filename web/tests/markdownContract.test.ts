import assert from 'node:assert/strict'
import { test } from 'node:test'

import { mdLiteToHtml } from '../src/lib/markdownLite.ts'

// 渲染契约：用户消息、助手回答、历史回放与流式终态共用同一解析与安全行为。
// 未达标用例为待实现契约；达标用例是渲染层演进时的回归钉。

test('契约·表格转义竖线：\\| 还原为字面竖线，不切列', () => {
  const md = '| 描述 | 值 |\n| --- | --- |\n| 字面竖线 | a\\|b |'
  const html = mdLiteToHtml(md)
  assert.ok(html.includes('<table'), html)
  assert.ok(html.includes('<td>a|b</td>'), html)
  assert.ok(!html.includes('a\\'), html)
})

test('契约·表格行内代码竖线：码内裸竖线不切列', () => {
  const md = '| 描述 | 值 |\n| --- | --- |\n| 行内代码 | `left|right` |'
  const html = mdLiteToHtml(md)
  assert.ok(html.includes('<code>left|right</code>'), html)
  const headerCells = (html.match(/<th /g) || []).length
  const bodyCells = (html.match(/<td/g) || []).length
  assert.equal(headerCells, 2, html)
  assert.equal(bodyCells, 2, html)
})

test('契约·任务列表：[ ] / [x] 产出 checkbox', () => {
  const html = mdLiteToHtml('- [ ] 待办\n- [x] 已完成')
  assert.ok(html.includes('type="checkbox"'), html)
  assert.ok(html.includes('disabled'), html)
  assert.ok(html.includes('checked'), html)
  assert.ok(!html.includes('[ ]'), html)
  assert.ok(!html.includes('[x]'), html)
})

test('契约·标题级别：h4-h6 真实渲染', () => {
  assert.equal(mdLiteToHtml('#### 四级'), '<h4>四级</h4>')
  assert.equal(mdLiteToHtml('##### 五级'), '<h5>五级</h5>')
  assert.equal(mdLiteToHtml('###### 六级'), '<h6>六级</h6>')
})

test('契约·多反引号围栏：外层四反引号不被内层三反引号闭合', () => {
  const md = '````md\n### 步骤\n```ts\nconst x = 1\n```\n收尾\n````'
  const html = mdLiteToHtml(md)
  const opens = (html.match(/<pre><code/g) || []).length
  assert.equal(opens, 1, html)
  assert.ok(html.includes('const x = 1'), html)
  assert.ok(html.includes('收尾'), html)
})

test('契约·斜体：*x* 与 _x_ 产出 em', () => {
  assert.equal(mdLiteToHtml('*斜体*'), '<p><em>斜体</em></p>')
  assert.equal(mdLiteToHtml('_斜体_'), '<p><em>斜体</em></p>')
})

test('契约·删除线：~~x~~ 产出删除标记', () => {
  assert.equal(mdLiteToHtml('~~删掉~~'), '<p><s>删掉</s></p>')
})

test('契约·链接：http(s) 可点击，javascript 与 data:text/html 被拒', () => {
  const ok = mdLiteToHtml('[说明](https://example.com/a?b=1)')
  assert.ok(ok.includes('<a href="https://example.com/a?b=1"'), ok)
  const js = mdLiteToHtml('[坏](javascript:alert(1))')
  assert.ok(!js.includes('<a'), js)
  const data = mdLiteToHtml('[坏](data:text/html,<script>alert(1)</script>)')
  assert.ok(!data.includes('href="data:text/html'), data)
})

test('契约·围栏语言标记：保留 language-* class', () => {
  const html = mdLiteToHtml('```ts\nconst x = 1\n```')
  assert.ok(html.includes('<code class="language-ts">'), html)
})

test('契约·嵌套列表：ol 内嵌 ul 保留层级', () => {
  const html = mdLiteToHtml('1. 甲\n   - 乙')
  assert.ok(html.includes('<ul><li>乙</li></ul></li></ol>'), html)
})

test('契约·单列表格：单列表识别为表格', () => {
  const html = mdLiteToHtml('| 单列 |\n| --- |\n| 甲 |')
  assert.ok(html.includes('<table'), html)
  assert.ok(html.includes('<td>甲</td>'), html)
})

test('回归钉·自有语法与既有行为在替换后保持', () => {
  assert.equal(mdLiteToHtml('现价 ==史低=='), '<p>现价 <mark>史低</mark></p>')
  assert.equal(mdLiteToHtml('## 现价结论'), '<h3>现价结论</h3>')
  assert.equal(mdLiteToHtml('- 甲\n· 乙\n* 丙'), '<ul><li>甲</li><li>乙</li><li>丙</li></ul>')
  assert.equal(mdLiteToHtml('1. 先查\n2、 再比'), '<ol><li>先查</li><li>再比</li></ol>')
})

test('回归钉·HTML 与危险协议全转义（替换后必须维持）', () => {
  assert.equal(mdLiteToHtml('<script>alert(1)</script> & "x"'), '<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;</p>')
  const img = mdLiteToHtml('<img src=x onerror=alert(1)>')
  assert.ok(!img.includes('<img'), img)
  assert.ok(img.includes('&lt;img'), img)
})

test('回归钉·SVG 消毒边界在替换后保持', () => {
  const svg = '<svg><rect onload="alert(1)" x="1" width="2" height="2" /><script>alert(2)</script></svg>'
  const html = mdLiteToHtml(svg)
  assert.ok(html.includes('<div class="pilot-svg"'), html)
  assert.ok(!html.includes('onload'), html)
  assert.ok(!html.includes('<script'), html)
})

test('契约·SVG 危险 CSS：expression( 调用形与 url(javascript:) 均拦截', () => {
  const expr = '<svg viewBox="0 0 10 10"><rect width="5" height="5" style="width: expression(alert(1));" /></svg>'
  assert.ok(!mdLiteToHtml(expr).includes('pilot-svg'), 'expression( 调用形必须被拦')
  const url = '<svg viewBox="0 0 10 10"><rect width="5" height="5" style="background: url(javascript:alert(1));" /></svg>'
  assert.ok(!mdLiteToHtml(url).includes('pilot-svg'), url)
})

test('回归钉·流式半截输入不崩溃且不闪原始标记', () => {
  const halfTable = '| 游戏 | 价格 |\n| :--'
  const halfFence = '```ts\nconst x ='
  const halfLink = '见 [文档](https://exam'
  for (const src of [halfTable, halfFence, halfLink]) {
    const html = mdLiteToHtml(src)
    assert.ok(!html.includes('undefined'), html)
    assert.ok(!html.includes('[object'), html)
  }
  assert.ok(!mdLiteToHtml(halfTable).includes('<table'), '分割线未完整时不出表')
})
