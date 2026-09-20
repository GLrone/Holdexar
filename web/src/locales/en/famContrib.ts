/* English 词典 · famContrib（与 zh-CN/famContrib.ts 同构，key 必须逐一对齐）。
   对应源文件：views/family/tabs/FamContrib.vue。

   措辞对齐（这些词的译法在别处已定，本页沿用）：
   · contribution split —— A 表「贡献分布」；family library —— A 表「家庭库」；
   · Exclusive / Shared by {n} / tier —— B 表「独占」「N人共享」「档位·分层」；
   · acquisition / acquired —— B 表「入库」（与 acquisition heatmap 同源）；
   · share —— B 表「占比」（纯比例用 share，不用 ratio）；
   · Exclusive contributions (filtered) —— D4：本页的独占数随范围控件变化，
     与 famLib.kpi.exclusive 的「全库口径」是两个数，英文上必须看得出差别；
   · X per member —— D3，`famContrib.kpi.perMember` 用 "Exclusive per member"，
     不用 "per-capita"（禁止 Contributors）。

   档位提示是两条整句而不是「标签 + 计数」两段拼接：中英语序不同，`tierLabel()`
   的结果塞进占位符里英文会拼成残句。 */

import type { MessageKey } from '../zh-CN'

const famContrib: Partial<Record<MessageKey, string>> = {
  /* Empty states */
  'famContrib.empty.loading': 'Loading family library…',
  'famContrib.empty.noData':
    'No family library data yet — sync your family group to see the contribution split',

  /* Member name fallback (last 4 digits of the steamid) */
  'famContrib.memberFallback': 'Member {id}',

  /* Range toggle */
  'famContrib.range.all': 'All time',
  'famContrib.range.allTip': 'Counts every acquisition in history',
  'famContrib.range.halfYear': 'Last 6 months',
  'famContrib.range.halfYearTip': 'Counts only acquisitions from the last 6 months',

  /* Member contribution stack */
  'famContrib.chart.title': 'Member contributions stacked by sharing tier',
  'famContrib.tier.exclusive': 'Exclusive',
  'famContrib.tier.shared': 'Shared by {n}',
  'famContrib.tier.tipExclusive': 'Exclusive: {count} titles',
  'famContrib.tier.tipShared': 'Shared by {members}: {count} titles',

  /* Contribution share donut */
  'famContrib.donut.title': 'Member contribution share',
  'famContrib.donut.total': 'Total acquired',

  /* Half-year acquisitions */
  'famContrib.halfYear.title': 'Acquisitions, last 6 months',
  'famContrib.halfYear.tip': '{month}: {count} titles',

  /* KPI cards */
  'famContrib.kpi.total': 'Family library games',
  'famContrib.kpi.exclusive': 'Exclusive contributions (filtered)',
  'famContrib.kpi.perMember': 'Exclusive per member',
  'famContrib.kpi.rate': 'Exclusive rate',
}

export default famContrib
