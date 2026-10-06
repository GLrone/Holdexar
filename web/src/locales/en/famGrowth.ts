/* English 词典 · famGrowth —— views/family/tabs/FamGrowth.vue（与 zh-CN/famGrowth.ts 同构，key 必须逐一对齐）。
   措辞沿用既有译法：acquisition / acquired / playtime / family library；
   贡献最多 / 贡献最少 → Top / Lowest contributor（一律 members 口径，不写 Contributors）。
   不变量：行内标记只有 <b> 与 class="gr-top" / "gr-low"（强调色与等宽字在 CSS 里由 :deep() 够进来）。 */

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
