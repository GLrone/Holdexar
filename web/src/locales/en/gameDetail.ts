/* English 词典 · gameDetail（与 zh-CN/gameDetail.ts 同构，key 必须逐一对齐）。

   ⚠️ 下列词条与 trendDrawer / trendChart 模块是**同一件事的镜像译法**，改动必须两边同步
   （门禁查不出跨模块同义分歧）：range.* / event.* / trend.lowest（= All-time low）/
   trend.discount / trend.noDiscount / trend.steamNow / trend.empty / trend.emptyRegion。
   与 gameCard 模块对齐的另有 rating.* / price.save / price.col.* / bundle.* / badge.hb /
   hl.* / pp.cut / region.cn / link.steamStore / price.locked / price.lowestTag / removed.tag。 */

import type { MessageKey } from '../zh-CN'

const gameDetail: Partial<Record<MessageKey, string>> = {
  /* Section anchors (the data-section attribute values) + their visible titles */
  'gameDetail.section.header': 'Game details',
  'gameDetail.section.priceDetail': 'All-region prices',
  'gameDetail.section.trend': 'Price history',
  'gameDetail.section.bundles': 'Linked bundles',

  /* Gradient header */
  'gameDetail.header.back': 'Back to the store',
  'gameDetail.header.developers': 'Developer: {names}',
  'gameDetail.header.publishers': 'Publisher: {names}',
  'gameDetail.region.cn': 'CN',
  'gameDetail.summary.lowestRegion': '{region} lowest',
  'gameDetail.summary.savings': 'You save',
  'gameDetail.hl.newLow': 'New low',
  'gameDetail.hl.sameLow': 'Ties low',
  'gameDetail.discount.endsAt': 'Ends {date}',
  'gameDetail.pp.cut': 'Price cut',
  'gameDetail.pp.raise': 'Base price raised',
  'gameDetail.link.steamStore': 'Steam Store',

  /* Delisted banner */
  'gameDetail.removed.tag': 'Delisted',
  'gameDetail.removed.judged': 'Removal confirmed {date}',
  'gameDetail.removed.retry': 'Recheck',
  'gameDetail.removed.probing': 'Checking…',
  'gameDetail.removed.requeued': 'Recrawl queued — refresh in a moment',
  'gameDetail.removed.cleared': 'Flag cleared — the next monitoring round will pick it up',

  /* Info sidebar */
  'gameDetail.info.basic': 'Basic info',
  'gameDetail.info.type': 'Type: {type}',
  'gameDetail.info.releaseDate': 'Released: {date}',
  'gameDetail.info.chinese': 'Chinese: {value}',
  'gameDetail.info.series': 'Series: {id}',
  'gameDetail.info.developers': 'Developers',
  'gameDetail.info.publishers': 'Publishers',
  'gameDetail.info.genres': 'Genres',
  'gameDetail.info.reviews': 'Reviews',
  'gameDetail.rating.positive': '{rate}% positive',
  'gameDetail.rating.reviews': '{n} reviews',
  'gameDetail.info.features': 'Features',

  /* Feature badges (the XGP one is a brand word plus a backend tier — no entry) */
  'gameDetail.badge.familySharing': 'Family Sharing',
  'gameDetail.badge.tradingCards': 'Trading Cards',
  'gameDetail.badge.adult': 'Adult content',
  'gameDetail.badge.visualNovel': 'Visual novel',
  'gameDetail.badge.epic': 'EPIC gave it away',
  'gameDetail.badge.epicDate': 'EPIC gave it away ({date})',
  'gameDetail.badge.hb': 'HB bundle',
  'gameDetail.badge.bundled': 'In {n} bundles',

  /* All-region price table */
  'gameDetail.price.col.region': 'Region',
  'gameDetail.price.col.native': 'Local price',
  'gameDetail.price.col.cny': 'CNY',
  'gameDetail.price.col.discount': 'Discount',
  'gameDetail.price.col.save': 'Savings',
  'gameDetail.price.top3': 'Cheapest 3',
  'gameDetail.price.medalTip': 'All-region lowest price ranking (excluding CN)',
  'gameDetail.price.lowestTag': 'Lowest',
  'gameDetail.price.locked': 'Locked',
  'gameDetail.price.free': 'Free',
  'gameDetail.promo.active': 'Free until {date}',
  'gameDetail.price.save': 'Save ¥{amount}',
  'gameDetail.price.empty': 'No price data available',

  /* Price history */
  'gameDetail.range.90d': '90 days',
  'gameDetail.range.1y': '1 year',
  'gameDetail.range.3y': '3 years',
  'gameDetail.range.all': 'All time',
  /* Region picker: name and currency are data, but the full-width parens are zh typography */
  'gameDetail.region.option': '{name} ({currency})',
  'gameDetail.trend.steamNow': 'Steam price',
  'gameDetail.trend.discount': '▼ -{pct}% · On sale',
  'gameDetail.trend.noDiscount': 'Full price',
  'gameDetail.trend.emptyRegion':
    'No price history for this region — the chart takes shape as scheduled crawls accumulate.',
  'gameDetail.trend.lowest': 'All-time low',
  'gameDetail.trend.debutPrice': 'Launch price',
  'gameDetail.trend.lowNodeCount': '{n} times',
  'gameDetail.trend.lowNodes': 'New lows',
  'gameDetail.trend.empty':
    'No price history yet — the chart takes shape as scheduled crawls accumulate.',
  /* All-time-low timeline markers */
  'gameDetail.event.first': 'First',
  'gameDetail.event.lowest': 'Lowest',
  'gameDetail.event.drop': 'Drop',

  /* Linked bundles */
  'gameDetail.bundles.title': 'Linked bundles ({n})',
  'gameDetail.bundle.completable': 'Can complete',
  'gameDetail.bundle.wholeOnly': 'Whole bundle only',
  'gameDetail.bundle.unknown': 'Status unknown',
}

export default gameDetail
