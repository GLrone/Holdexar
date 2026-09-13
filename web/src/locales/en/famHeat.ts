/* English 词典 · famHeat（与 zh-CN/famHeat.ts 同构，key 必须逐一对齐）。
   对应源文件：views/family/tabs/FamHeat.vue。

   措辞对齐（这些词的译法在别处已定，本页沿用）：
   · acquisition / acquired —— B 表「入库」，与 A 表 acquisition heatmap 同源；
   · Snapshot —— B 表「快照数据」（代码 `fromSnapshot`）；
   · family library —— A 表「家庭库」；
   · member / members —— A 表与 D9（不用 Contributors）。

   月份轴标签不在词典里：走 `useLocaleFormat()`（zh `1月` / en `Jan`，U3）。
   `YYYY-MM-DD` 的日期格式同样保持原样——两种语言下是同一串 ISO 文本。 */

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
