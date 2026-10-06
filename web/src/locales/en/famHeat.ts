/* English 词典 · famHeat —— views/family/tabs/FamHeat.vue（与 zh-CN/famHeat.ts 同构，key 必须逐一对齐）。
   措辞沿用既有译法：acquisition / acquired / Snapshot / family library / member(s)（不用 Contributors）。
   不变量：月份轴标签不进词典，走 useLocaleFormat()（zh「1月」/ en "Jan"）；
   YYYY-MM-DD 日期格式保持原样（两种语言同一串 ISO 文本）。 */

import type { MessageKey } from '../zh-CN'

const famHeat: Partial<Record<MessageKey, string>> = {
  /* Empty states */
  'famHeat.empty.loading': 'Loading family library…',
  'famHeat.empty.noData':
    'No acquisition dates yet — shown once Steam returns rt_time_acquired for a synced family library',
  'famHeat.empty.member': 'No acquisition dates for this member',

  /* Member filter and titles */
  'famHeat.memberFallback': 'Member {id}',
  'famHeat.filter.all': 'All members',
  'famHeat.title.family': 'Family library acquisition activity',
  'famHeat.tag.snapshot': 'Snapshot',

  /* Block meta */
  'famHeat.meta': '<b>{total}</b> acquisitions · peak {peak}/day',
  'famHeat.tip.day': '{date} · {count} acquired',

  /* Legend and reading notes */
  'famHeat.legend.low': 'Less',
  'famHeat.legend.high': 'More',
  'famHeat.note':
    'How to read: one cell = how many games were acquired that day (Steam rt_time_acquired); one column = one week (Mon → Sun); shading is a relative scale against the peak. The range adapts on its own — from the first acquisition to today — so history is kept in full.',
}

export default famHeat
