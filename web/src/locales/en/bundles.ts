/* English 词典 · bundles（与 zh-CN/bundles.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const bundles: Partial<Record<MessageKey, string>> = {
  /* Drawer completion bar */
  'bundles.status.owned': '✅ You already own everything',
  'bundles.status.family': '✅ Your family library covers it all',

  /* Must-purchase-as-set labels (card tags + status bar share these) */
  'bundles.mps.completable': '✅ Completable',
  'bundles.mps.setOnly': '❌ Not completable',
  'bundles.mps.unknown': '❓ Unknown',
  'bundles.mps.completableTip':
    'Buy only the missing items and still get the bundle base discount',
  'bundles.mps.setOnlyTip': 'Must be bought as a whole bundle',

  /* Game-name suffixes (leading space is intentional) */
  'bundles.gameTag.owned': ' [Owned]',
  'bundles.gameTag.family': ' [Family: {owners}]',
  'bundles.gameTag.wishlist': ' [Wishlist: {owners}]',

  /* List header */
  'bundles.head.total': '{n} bundles',
  'bundles.head.completable': '· {n} completable',
  'bundles.empty.noData': 'No bundle data',
  'bundles.empty.noMatch': 'No matching bundles',
  'bundles.nav.search': 'Search bundles… (Enter)',
  'bundles.list.loadMore': 'Load more ({n} remaining)',

  /* Region dimension (diff re-anchored to the selected region) */
  'bundles.regionMode.all': 'All',
  'bundles.regionMode.cheaper': 'Cheaper there',
  'bundles.regionMode.locked': 'Unavailable there',

  /* Advanced filter (only dimensions with a bundle data source) */
  'bundles.filter.section.basic': 'Purchase shape',
  'bundles.filter.completableOnly': 'Completable only (no forced full set)',
  'bundles.filter.cnLowestOnly': 'CN is lowest',
  'bundles.filter.section.ownership': 'Ownership match',
  'bundles.filter.hideOwned': 'Hide owned',
  'bundles.filter.hideFamily': 'Hide family-shared',
  'bundles.filter.section.price': 'Price (CN)',
  'bundles.filter.price': 'CN price',
  'bundles.filter.diffMinLabel': 'Min diff',
  'bundles.filter.min': 'min',
  'bundles.filter.max': 'max',
  'bundles.filter.cny': '¥',
  'bundles.filter.diffMinRegionHint': 'Region selected: measured against that region',

  /* Ownership badge on the cover */
  'bundles.badge.me': 'Me',
  'bundles.badge.owned': 'Owned',
  'bundles.badge.family': 'Family',

  /* Card price block */
  'bundles.tag.baseDiscount': 'Base discount {pct}%',
  'bundles.link.store': 'Steam Store',
  'bundles.price.none': 'N/A',
  'bundles.price.lowest': 'Lowest',
  'bundles.price.diff': 'Diff',
  'bundles.price.save': 'Save {amt}',
  'bundles.price.noDiff': 'No diff',
  'bundles.action.allRegionPrices': 'All regions',

  /* Detail drawer */
  'bundles.action.steamStore': 'Steam Store',
  'bundles.action.calc': '✨ Calculate bundle price',
  'bundles.drawer.statusLabel': 'Completion:',
  'bundles.drawer.lockHint': 'ℹ️ A pale yellow background means some games are region-locked',
  'bundles.drawer.detailError': 'Failed to load details: {err}',
  'bundles.drawer.giftTooltip': 'Click to see gifting analysis for {region}',
  'bundles.drawer.lockedTip': '{n} games are region-locked here',
  'bundles.drawer.lockedBadge': '{n} locked',
  'bundles.drawer.locked': 'Region locked',

  /* Gifting analysis */
  'bundles.gift.head': '🎁 Gifting target: {region}',
  'bundles.gift.collapse': 'Collapse',
  'bundles.gift.canGive': '✅ Can gift to (any unlocked region)',
  'bundles.gift.cannotGive': '⛔ Cannot gift to',
  'bundles.gift.canReceive': '📥 Can receive from',

  /* Games in the bundle */
  'bundles.games.title': '🎮 Games in this bundle',
  'bundles.games.empty': 'No game data',
  'bundles.games.viewAll': 'View all {n} games',

  /* Region AppID differences (AGR) */
  'bundles.agr.title': 'Region AppID differences:',
  'bundles.agr.count': '{n} games',

  /* "All games / AGR" dialog titles */
  'bundles.dialog.allGamesTitle': '🎮 Bundle includes {n} games',
  'bundles.dialog.agrTitle': '🎮 Region variant ({regions}) — {n} games',

  /* Completion calculator */
  'bundles.calc.title': '🧮 Bundle price calculator',
  'bundles.calc.region': 'Region:',
  'bundles.calc.foreignTotal': 'Estimated foreign total:',
  'bundles.calc.cnyTotal': 'Total in CNY:',
  'bundles.calc.run': 'Calculate',
  /* 整句一条，行内强调用 <em> 包在值里（组件侧 v-html）——见 zh-CN/bundles.ts 的说明：
     曾切成 Prefix/Word/Suffix 三段，英文那样拼是残句，缺 "to buy"。 */
  'bundles.calc.excludeHint':
    '💡 Select the games below that you <em class="calc-hint-em">do not want to buy</em>',
  'bundles.calc.autoPreselect':
    '(Games you already own and items with no price data are pre-selected)',
  'bundles.calc.noValidItems': 'No valid items',
  'bundles.calc.noPrice': 'No price yet',
  'bundles.calc.noGames': 'No game details for this region',

  /* Region-lock mask */
  'bundles.mask.regionLocked': '🔒 Locked',
}

export default bundles
