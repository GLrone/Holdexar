/* English 词典 · toolbox（与 zh-CN/toolbox.ts 同构，key 必须逐一对齐）。

   两处刻意的写法：
   · `bill.stats.txUnit` 留空 —— 中文「12 笔」的量词在英文里没有对等物，
     数字旁的独立小字号 span 渲染成空串，等于英文只显示数字（No unused word）。
   · `bill.trend.monthLabel` 的英文只有月份数字 —— 柱下标在中文是「3月」，
     英文侧靠同一区块的标题（Spending, last 8 months）表明这是月度视图。 */

import type { MessageKey } from '../zh-CN'

const toolbox: Partial<Record<MessageKey, string>> = {
  /* Section anchors (data-section value = module title) */
  'toolbox.section.bill': 'Account spending',
  'toolbox.section.cdk': 'Bulk CDK activation',

  /* Account switcher */
  'toolbox.account.noNickname': 'Nickname not synced',
  'toolbox.account.optionLabel': '{name} · Friend code {code}',
  'toolbox.account.switched': 'Account switched — the activation quota starts over for it',

  /* Account spending (bills) */
  'toolbox.bill.subtitle':
    'Purchases filterable by type · Multi-currency amounts converted to CNY at the posting-day rate · Latest 5 in this summary, everything else under Full bills',
  'toolbox.bill.loading': 'Loading bill profiles…',
  'toolbox.bill.loadFailed': 'Could not load bills: {msg}',
  'toolbox.bill.empty.title': 'No bill data yet',
  'toolbox.bill.empty.hint':
    'Bills sync automatically once a Steam cookie is linked; full details are on the Full bills page',
  'toolbox.bill.empty.action': 'Go to full bills',
  'toolbox.bill.importChip': '{nick} ({amount} CNY)',

  /* Four mini-stats */
  'toolbox.bill.stats.net': 'Net spend (refunds deducted)',
  'toolbox.bill.stats.txCount': 'Purchases',
  'toolbox.bill.stats.txUnit': '',
  'toolbox.bill.stats.month': 'This month',
  'toolbox.bill.stats.refund': 'Total refunds',

  /* Transaction types — shared by the filter chips, the type column and the ratio rows */
  'toolbox.bill.type.game': 'Game purchase',
  'toolbox.bill.type.wallet': 'Wallet top-up',
  'toolbox.bill.type.market': 'Market purchase',
  'toolbox.bill.type.gift': 'Gift purchase',
  'toolbox.bill.type.giftShort': 'Gift',
  'toolbox.bill.type.refund': 'Refund',

  /* Summary table */
  'toolbox.bill.table.date': 'Date',
  'toolbox.bill.table.item': 'Item',
  'toolbox.bill.table.type': 'Type',
  'toolbox.bill.table.amount': 'Amount',
  'toolbox.bill.table.noRows': 'No transactions in this category',
  'toolbox.bill.viewAll': 'View all',

  /* 8-month trend bars + category ratios */
  'toolbox.bill.trend.title': 'Spending, last 8 months (CNY)',
  'toolbox.bill.trend.monthLabel': '{month}',
  'toolbox.bill.trend.empty': 'No monthly data yet',
  'toolbox.bill.ratio.title': 'Spending by category',

  /* Bulk CDK activation */
  'toolbox.cdk.subtitle':
    'Calls the real Steam activation endpoint (signed-in cookie) · 9 keys per batch · 20s between batches · Results carry SubID + edition name',

  'toolbox.cdk.needCookie.title': 'Activation needs a signed-in Steam cookie',
  'toolbox.cdk.needCookie.hint':
    'Link a cookie on the Me page (or use “Sign in and fetch automatically”) and activate in bulk right here',
  'toolbox.cdk.needCookie.action': 'Link it on the Me page',
  'toolbox.cdk.quotaMissing':
    'No Steam cookie linked yet — link one on the Me page before activating (activation needs a signed-in session)',
  'toolbox.cdk.bindFirst': 'Link a Steam cookie on the Me page before activating',

  /* Input and progress */
  'toolbox.cdk.input.title': 'Activation keys (one per line)',
  'toolbox.cdk.input.placeholder':
    'Bulk activation: paste any block of text and XXXXX-XXXXX-XXXXX keys are picked out automatically',
  'toolbox.cdk.start': 'Start',
  'toolbox.cdk.reset': 'Reset',
  'toolbox.cdk.batch.label': 'Batch {current} / {total}',

  'toolbox.cdk.progress.ready': 'Ready · not started',
  'toolbox.cdk.progress.running': 'Activating',
  'toolbox.cdk.progress.batchWait': 'Waiting 20s between batches (Steam attempt limit)',
  'toolbox.cdk.progress.done': 'Activation complete',
  'toolbox.cdk.progress.limitReached':
    'This account has hit its activation limit (10 per 30 minutes) — switch accounts to start over, or try again later',
  'toolbox.cdk.progress.noCookie': 'No Steam cookie linked',

  'toolbox.cdk.limit.hint':
    'Activation limit is per account: 10 attempts per 30 minutes, success or not · switching accounts resets it · up to 9 keys per batch',
  'toolbox.cdk.limit.used': 'Used {used} / {total} on this account',

  /* Queue */
  'toolbox.cdk.queue.title': 'Activation queue',
  'toolbox.cdk.queue.empty': 'Paste keys on the left and the queue shows up here',

  /* Statuses */
  'toolbox.cdk.status.wait': 'Waiting',
  'toolbox.cdk.status.doing': 'Activating',
  'toolbox.cdk.status.queueOk': 'Activated',
  'toolbox.cdk.status.ok': 'Success',
  'toolbox.cdk.status.own': 'Already owned',
  'toolbox.cdk.status.fail': 'Failed',
  'toolbox.cdk.summary.fail': 'Invalid / failed',
  'toolbox.cdk.summary.own': 'Owned / duplicate',

  /* Results table */
  'toolbox.cdk.result.title': 'Activation results',
  'toolbox.cdk.result.copyTitle': 'Copy all SubIDs + edition names (one per line)',
  'toolbox.cdk.result.col.index': '#',
  'toolbox.cdk.result.col.result': 'Result',
  'toolbox.cdk.result.col.detail': 'Details',
  'toolbox.cdk.result.col.game': 'Game / edition',
  'toolbox.cdk.raw.expand': 'Show the raw Steam response',
  'toolbox.cdk.raw.collapse': 'Hide the raw Steam response',
  'toolbox.cdk.failNoResult': 'Activation failed (no result in the response)',

  /* Completion toasts */
  'toolbox.cdk.done.ok': 'Done: {ok} activated · {own} already owned',
  'toolbox.cdk.done.okSkipped':
    'Done: {ok} activated · {own} already owned · {skipped} skipped (quota)',
  'toolbox.cdk.done.partial': 'Done: {ok} activated · {own} already owned · {fail} failed',
  'toolbox.cdk.done.partialSkipped':
    'Done: {ok} activated · {own} already owned · {fail} failed · {skipped} skipped',
  'toolbox.cdk.done.allFailed': 'Every activation failed ({fail}) — see the result list',
  'toolbox.cdk.done.allFailedSkipped':
    'Every activation failed ({fail}) — see the result list · {skipped} skipped (quota)',
}

export default toolbox
