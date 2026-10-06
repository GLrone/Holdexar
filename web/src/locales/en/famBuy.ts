/* English 词典 · famBuy —— views/family/tabs/FamBuy.vue（与 zh-CN/famBuy.ts 同构，key 必须逐一对齐）。
   措辞沿用既有译法：Acquired（购入 / 入库同一字段 rt_time_acquired，不写 Date / Purchase date）/ Free / Snapshot /
   Exclusive only / Bills（沿用 shell.nav.bills，本按钮正跳到 /bills）/ family library。 */

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
