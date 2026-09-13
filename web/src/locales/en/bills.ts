/* English 词典 · bills（与 zh-CN/bills.ts 同构，key 必须逐一对齐）。
   对应源文件：views/bills/Index.vue + LedgerTab.vue + CdkTab.vue + TopupTab.vue。
   子区块用子命名空间区分：bills.ledger.* / bills.cdk.* / bills.topup.*。

   金额一律留在组件侧用 useLocaleFormat() 格式化后作为 {amount} 传入——词条里的
   ¥ 是标签的一部分（同 gameCard.price.save / trendChart.tip.* 的写法），不带数字。

   带行内标记的三条（bills.empty.desc / bills.ledger.year.sumRefund /
   bills.topup.year.sum）保持 HTML 与 zh 侧对齐：组件侧 v-html 渲染，标记以外的
   词句按英文重写，不逐字对译。

   量词：zh 用「笔 / 条 / 个」，英文没有对应计数词，按被数对象写成
   transactions / orders / top-ups / entries。 */

import type { MessageKey } from '../zh-CN'

const bills: Partial<Record<MessageKey, string>> = {
  /* Shared across files */
  'bills.fxMissing': 'No FX rate',
  'bills.year.all': 'All years',
  'bills.currencyValue': '{name} ({code})',

  /* Section anchors (data-section carries these keys verbatim) */
  'bills.section.overview': 'Account overview',
  'bills.section.stats': 'Spending stats',
  'bills.section.chart': 'Spending trend',
  'bills.section.ledger': 'Transaction ledger',

  /* Tabs */
  'bills.tab.ledger': 'Transactions',
  'bills.tab.topup': 'Top-ups',
  'bills.tab.cdk': 'Licenses',

  /* Sync */
  'bills.sync.inProgress': 'Syncing…',
  'bills.sync.cta': 'Sync bills now',
  'bills.sync.ctaShort': 'Sync bills',
  'bills.sync.success':
    'Synced “{nickname}”: {bills} transactions · {cdk} CDK/gift entries ({rows} history rows)',
  'bills.sync.autoRunning': 'Auto-sync in progress…',
  'bills.sync.lastFailed': 'Last sync failed',
  'bills.sync.errorRetry': '{error} (retrying every 30 min)',
  /* Progress bubble stages (Steam cursor pagination discloses no page total) */
  'bills.sync.stageIdentity': 'Verifying account…',
  'bills.sync.stageHistory': 'Fetching transactions · page {pages} · {rows} rows',
  'bills.sync.stageLicenses': 'Fetching licenses…',
  'bills.sync.stageImport': 'Saving to database…',

  /* Delete a bill */
  'bills.delete.success': 'Deleted the bill for “{nickname}”',
  'bills.action.delete': 'Delete',

  /* Empty state */
  'bills.empty.title': 'No bills yet',
  'bills.empty.desc':
    'Bind a Steam cookie under <b>Settings → Accounts</b> and bills are pulled in automatically (full history pagination + full license pagination, no browser extension needed).<br />Every line is converted to CNY at the <b>exchange rate of the transaction date</b>: refunds are credited back, wallet top-ups get their own stream, and gifts/CDKs are priced by hand.',

  /* Account header */
  'bills.hero.syncedAt': 'Synced {date}',
  'bills.hero.txCount': '{count} transactions',
  'bills.hero.txCountTip': 'Transaction count parsed by the server',
  'bills.hero.fxMissing': '{count} missing FX rates',
  'bills.hero.valueLabel': 'Account value',
  'bills.hero.valueFormula': '= self-purchases + CDK/gifts',
  'bills.hero.selfNet': 'Self-purchases {amount}',
  'bills.hero.cdkTotal': 'CDK/gifts {amount}',

  /* Stat cards */
  'bills.stats.net.label': 'Net spending',
  'bills.stats.net.sub': 'Purchases − refunds',
  'bills.stats.spend.label': 'Total purchases',
  'bills.stats.spend.sub': '{count} orders',
  'bills.stats.refund.label': 'Total refunds',
  'bills.stats.refund.sub': 'Refund rate {rate}',
  'bills.stats.quota.label': 'Gifting quota',
  'bills.stats.quota.sub': 'Self-purchases − gifts sent',
  'bills.stats.quota.tip':
    'Self-purchases {self} − gifts sent {gift}; a negative value means gifts exceed what you kept',
  'bills.stats.topup.label': 'Net top-ups',
  'bills.stats.topup.sub': '{count} top-ups',

  /* Account switch chips */
  'bills.accounts.fallbackName': 'Bill #{id}',
  'bills.accounts.confirmDelete': 'Delete the bill for “{nickname}” and all of its transactions?',
  'bills.accounts.deleteTip': 'Delete this bill',

  /* Spending trend card */
  'bills.chart.title': 'Spending trend',
  'bills.chart.desc': 'Bars = net spending per month (refunds shown negative), line = cumulative net spend.',
  'bills.chart.empty': 'No data for this year',
  /* Legend entries. Wording is kept identical to bills.chart.desc above
     ("net spending per month" / "cumulative net spend") so the legend and the
     caption name the same two series the same way. */
  'bills.chart.legend.net': 'Net spending',
  'bills.chart.legend.cum': 'Cumulative net spend',
  'bills.year.value': '{year}',

  /* Shared filter chip */
  'bills.filter.all': 'All',

  /* ═══ Transaction ledger (LedgerTab.vue) ═══ */
  'bills.ledger.warn.more': 'and {count} more',
  'bills.ledger.type.purchase': 'Purchases',
  'bills.ledger.type.gift': 'Gifts',
  'bills.ledger.type.ingame': 'In-game',
  'bills.ledger.type.refund': 'Refunds',
  'bills.ledger.search': 'Search games…',
  'bills.ledger.count': '{count} transactions',
  'bills.ledger.empty': 'No matching transactions',
  'bills.ledger.year.sum': 'Net <b>¥{net}</b> · {count} transactions',
  'bills.ledger.year.sumRefund':
    'Net <b>¥{net}</b> · refunds <span class="neg">-¥{refund}</span> · {count} transactions',
  'bills.ledger.month.label': '{month}',
  'bills.ledger.month.net': '¥{amount} · {count} transactions',
  'bills.ledger.detail.type': 'Type',
  'bills.ledger.detail.currency': 'Currency',
  'bills.ledger.detail.amount': 'Amount',
  'bills.ledger.detail.rate': 'Exchange rate',
  'bills.ledger.detail.rateValue': '1 {code} = {rate} CNY',
  'bills.ledger.detail.discount': 'Discount',
  'bills.ledger.detail.origPrice': 'Original price',
  'bills.ledger.detail.payment': 'Payment method',
  'bills.ledger.detail.ownership': 'Source',
  'bills.ledger.detail.giftRefund': 'Gift refund',

  /* ── Hit strips (shared by CdkTab + LedgerTab: family-library match + price context) ── */
  'bills.hit.stat': 'Library hits',
  'bills.hit.atPrice': 'At the time {amount}',
  'bills.hit.lowest': 'Lowest before {amount}',
  'bills.hit.atLowest': 'Historic low at the time {amount}',
  'bills.hit.openDetail': 'Open game details',

  /* ═══ Licenses (CdkTab.vue; the family page's "Library licenses" module merged in) ═══ */
  'bills.cdk.stats.priced': 'Priced',
  'bills.cdk.stats.total': 'Total priced',
  'bills.cdk.hint':
    'Entries with no amount in the bill (retail CDKs and received gifts) can be priced here — account value updates with them. Free licenses are listed only and never priced.',
  'bills.cdk.type.cdk': 'CDK {count}',
  'bills.cdk.type.gift': 'Gifts {count}',
  'bills.cdk.type.free': 'Free {count}',
  'bills.cdk.type.unpriced': 'Not priced',
  'bills.cdk.search': 'Search names…',
  'bills.cdk.count': '{count} entries',
  'bills.cdk.empty':
    'No license entries recorded (Steam Store purchases live under Transactions and are not duplicated)',
  'bills.cdk.price.placeholder': 'Not priced',
  'bills.cdk.price.unit': '¥ paid',
  'bills.cdk.free.mark': 'Free · display only',
  'bills.cdk.link.store': 'Store',
  'bills.cdk.save.success': 'Priced “{name}” at ¥{amount}',

  /* ═══ Wallet top-ups (TopupTab.vue) ═══ */
  'bills.topup.stats.total': 'Total top-ups',
  'bills.topup.stats.refund': 'Top-up refunds',
  'bills.topup.stats.net': 'Net top-ups',
  'bills.topup.stats.currencies': 'Currencies',
  'bills.topup.card.sub': '≈ ¥{amount} · {count} top-ups',
  'bills.topup.empty': 'No wallet top-ups for this account',
  'bills.topup.year.sum': 'Net <b>¥{amount}</b> · {count} top-ups',
  'bills.topup.badge.refund': 'Refund',
  'bills.topup.desc.default': 'Wallet top-up',
}

export default bills
