/* English 词典 · famLib —— views/gamelib/tabs/FamilyLib.vue（与 zh-CN/famLib.ts 同构，key 必须逐一对齐）。
   措辞沿用既有译法：Not listed / Exclusive / Shared / Last played / playtime / Family library；
   共享清单写 shared library，与 family library 刻意区分（两个集合）；
   "Acquired in last 30 days" 按真实口径（timeAcquired 入库数，不用 active）；
   Exclusive titles 为全库口径，与 FamContrib 的筛选口径英文上必须可区分。
   不变量：时长单位 h / kh 落 common.hours / common.hoursK，不在本模块。 */

import type { MessageKey } from '../zh-CN'

const famLib: Partial<Record<MessageKey, string>> = {
  /* Empty states */
  'famLib.empty.bindHint':
    'Once linked, click “⟳ Sync family” at the top of the Family page — the family library then aggregates the shared library plus members’ owned games.',
  'famLib.empty.loading': 'Loading family library…',
  'famLib.empty.noData': 'No family library data',
  'famLib.empty.noDataHint':
    'After joining a Steam Family and clicking “⟳ Sync family”, this view shows the shared library ∪ members’ owned games.',

  /* KPI cards */
  'famLib.kpi.total': 'Family library games',
  'famLib.kpi.exclusive': 'Exclusive titles',
  'famLib.kpi.shared': 'Shared',
  'famLib.kpi.active30': 'Acquired in last 30 days',
  'famLib.kpi.playtime': 'Total playtime',
  'famLib.kpi.value': 'Family library value (CN price)',

  /* Sort toolbar */
  'famLib.toolbar.searchPlaceholder': 'Search family library…',
  'famLib.toolbar.sortLabel': 'Sort:',
  'famLib.toolbar.onlyExclusive': 'Exclusive only',
  'famLib.sort.name': 'Name A-Z',
  'famLib.sort.playtime': 'Playtime',
  'famLib.sort.price': 'Price',

  'famLib.label.lastPlayed': 'Last played',

  /* Cards */
  'famLib.card.notListed': 'Not listed',
  'famLib.card.free': 'Free',
  'famLib.card.exclusive': 'Exclusive',
  'famLib.card.sharedBy': 'Shared by {n}',
  'famLib.card.playtime': 'Playtime',
  'famLib.card.cnPrice': 'CN price',

  /* Grid and pager */
  'famLib.grid.noMatch': 'No matching games in the family library',
  'famLib.pager.range': 'Showing {from}-{to} of <b>{n}</b> games',
  'famLib.pager.page': 'Page {page} / {total}',
  'famLib.pager.prev': 'Prev',
  'famLib.pager.next': 'Next',

  'famLib.action.refresh': 'Refresh family library (re-pulls Steam)',
}

export default famLib
