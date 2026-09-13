/* English 词典 · famLib（与 zh-CN/famLib.ts 同构，key 必须逐一对齐）。
   对应源文件：views/gamelib/tabs/FamilyLib.vue（自 family 页迁入）。

   措辞对齐（这些词的译法在别处已定，本页沿用）：
   · Not listed —— A 表（gameCard.cdk.notListed 的既有英文）；
   · Exclusive / Shared —— B 表「独占」「多人共享」；
   · Last played / playtime —— B 表与 D5（Steam 官方字段措辞）；
   · Family library —— A 表「家庭库」；D1 裁定它与 shared library 不是同一集合，
     故本页提到「共享清单」时写 shared library，不与 family library 混用；
   · Acquired in last 30 days —— D6：该 KPI 数的是 timeAcquired（入库）而非游玩，
     中文名「近30日活跃」是误导，英文按真实口径写，不用 active；
   · Exclusive titles —— D4：本页是全库独占数，与 FamContrib 的筛选口径区分；
   · Total playtime —— D2：三处中文指同一个数，英文一律这一种写法。

   时长单位（`h` / `kh`）不在这里：它不是语言中立的，跨模块共用，
   落在 common.hours（`{h} hrs`）与 common.hoursK（`{h}k hrs`）。 */

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
