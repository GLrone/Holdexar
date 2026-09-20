/* English 词典 · famGrowth（与 zh-CN/famGrowth.ts 同构，key 必须逐一对齐）。
   对应源文件：views/family/tabs/FamGrowth.vue。

   措辞对齐：
   · acquisition / acquired —— B 表「入库」；playtime —— B 表「时长」；
   · family library —— A 表「家庭库」；
   · 贡献最多 / 贡献最少 → Top / Lowest contributor（成员一律
     members 口径，不写 Contributors 之外的花样）。

   行内标记只有 `<b>` 与 `class="gr-top"` / `class="gr-low"`（强调色与等宽字在
   CSS 里，由 `:deep()` 够进来——v-html 注入的节点拿不到 scoped 属性）。 */

import type { MessageKey } from '../zh-CN'

const famGrowth: Partial<Record<MessageKey, string>> = {
  'famGrowth.member.fallback': 'Member {id}',

  'famGrowth.empty.loading': 'Loading family library…',
  'famGrowth.empty.noData': 'No acquisition dates yet — the growth trend needs rt_time_acquired',

  'famGrowth.chart.title': 'Cumulative monthly acquisitions',
  'famGrowth.legend.total': 'Total',

  'famGrowth.compare.top': 'Top contributor: <b class="gr-top">{name}</b> ({n})',
  'famGrowth.compare.avg': 'Average: <b>{n}</b>',
  'famGrowth.compare.low': 'Lowest contributor: <b class="gr-low">{name}</b> ({n})',

  'famGrowth.span':
    'Total: <b>{n}</b> acquisitions · first: <b>{first}</b> → latest: <b>{last}</b>',
}

export default famGrowth
