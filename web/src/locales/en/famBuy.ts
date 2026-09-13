/* English 词典 · famBuy（与 zh-CN/famBuy.ts 同构，key 必须逐一对齐）。
   对应源文件：views/family/tabs/FamBuy.vue。

   措辞对齐：
   · Acquired —— B 表 / D5：「购入时间」与「入库时间」是同一个字段
     （rt_time_acquired），一律 Acquired，不写 Date / Purchase date；
   · Free / Snapshot / Exclusive only —— B 表；
   · Bills —— 沿用 shell.nav.bills 的既有英文（本按钮正跳到 /bills）；
   · family library —— A 表「家庭库」。 */

import type { MessageKey } from '../zh-CN'

const famBuy: Partial<Record<MessageKey, string>> = {
  /* Member filter (HlSelect options) */
  'famBuy.member.all': 'All buyers',
  'famBuy.member.fallback': 'Member {id}',

  'famBuy.card.free': 'Free',

  /* Empty states */
  'famBuy.empty.loading': 'Loading family library…',
  'famBuy.empty.noData': 'No acquisition dates yet — sync the family library to see purchase activity',

  /* Toolbar */
  'famBuy.toolbar.count':
    '<b>{n}</b> games · page <b>{page}/{total}</b>',
  'famBuy.toolbar.snapshot': 'Snapshot',
  'famBuy.toolbar.onlyExclusive': 'Exclusive only',
  'famBuy.action.bills': 'Full bill analysis',
  'famBuy.view.table': 'Table',
  'famBuy.view.cover': 'Cover',

  /* Table */
  'famBuy.table.name': 'Game',
  'famBuy.table.acquired': 'Acquired',
  'famBuy.table.buyer': 'Buyer',
  'famBuy.table.owners': 'Owners',
  'famBuy.table.cnPrice': 'CN price',
  /* 中文的「人」是量词，英文表头已写 Owners，单元格只需那个数 */
  'famBuy.table.ownerCount': '{n}',

  /* Footnote and pager */
  'famBuy.footer.legend':
    'Highlight/NEW = acquired within 30 days and exclusive; buyer = the member who acquired it most recently',
  'famBuy.pager.prev': 'Prev',
  'famBuy.pager.next': 'Next',
}

export default famBuy
