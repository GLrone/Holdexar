/* English 词典 · famContrib —— views/family/tabs/FamContrib.vue（与 zh-CN/famContrib.ts 同构，key 必须逐一对齐）。
   措辞沿用既有译法：contribution split / family library / Exclusive / Shared by {n} / tier / acquisition / share（纯比例用 share 不用 ratio）；
   Exclusive contributions (filtered) 随范围控件变化，与 famLib.kpi.exclusive（全库口径）英文上必须可区分；
   "Exclusive per member"（禁 per-capita 与 Contributors）。
   不变量：档位提示是整句词条，tierLabel() 的结果不塞占位符（拼接出残句）。 */

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
