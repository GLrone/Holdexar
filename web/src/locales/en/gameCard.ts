/* English 词典 · gameCard（与 zh-CN/gameCard.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const gameCard: Partial<Record<MessageKey, string>> = {
  /* Discount / all-time-low badges */
  'gameCard.hl.newLow': 'New low',
  'gameCard.hl.sameLow': 'Ties low',
  'gameCard.hl.notLow': 'Not a low',

  /* Discount badge hover tooltip */
  'gameCard.discount.endsAt': 'Ends {date}',

  /* EPIC / HB badges */
  'gameCard.epic.given': 'EPIC gave it away on {date}',
  'gameCard.hb.bundle': 'HB bundle',

  /* Delisted badge and tooltip */
  'gameCard.removed.tag': 'Delisted',
  'gameCard.removed.tip': 'Removed from Steam (confirmed {date})',

  /* Ownership badge / hover tooltip titles */
  'gameCard.status.owned': 'Owned',
  'gameCard.status.ownedTitle': 'Owned by',
  'gameCard.status.family': 'Family',
  'gameCard.status.familyTitle': 'Shared from',
  'gameCard.status.wishlist': 'Wishlist',
  'gameCard.status.wishlistTitle': 'Wishlisted by',
  'gameCard.status.wishlistMore': 'Wishlist +{n}',

  /* Account name fallback in the ownership tooltip */
  'gameCard.owner.unknown': 'Unknown',

  /* Follow star (state lives in the backend tracking pool; the backend detail
     takes precedence on failure, this is the fallback) */
  'gameCard.follow.tip': 'Follow / unfollow',
  'gameCard.follow.fail': 'Failed to update follow',

  /* Rating and review count */
  'gameCard.rating.positive': '{rate} positive',
  'gameCard.rating.none': 'No rating',
  'gameCard.reviewCount': '{n} reviews',

  /* Attribute tags (hover tooltips) */
  'gameCard.tag.familySharing': 'Supports Family Sharing',
  'gameCard.tag.tradingCards': 'Includes Steam Trading Cards',
  'gameCard.tag.ppCut': 'Price cut',
  'gameCard.tag.ppCutTip': 'CN base price was permanently lowered',

  /* Steam store link label */
  'gameCard.steamLink': 'Steam Store',

  /* Price block */
  'gameCard.price.cn': 'CN',
  'gameCard.price.free': 'Free',
  'gameCard.price.cnLowest': 'CN lowest',
  'gameCard.price.diff': 'Price gap',
  'gameCard.price.save': 'Save ¥{amount}',
  'gameCard.price.noDiff': 'Same price',

  /* Card action row */
  'gameCard.regionPrice.title': 'All regions',
  'gameCard.trend.tip': 'Price history',
  'gameCard.trend.label': 'Trend',

  /* GPW popover tabs (icon button titles) */
  'gameCard.tabs.list': 'List',
  'gameCard.tabs.chart': 'Chart',

  /* Region cell states */
  'gameCard.region.unavailableTip':
    'Price update failed for this region (crawl error); it will be retried in the next round',
  'gameCard.region.clickGiftTip': 'Click to analyze gifting',
  'gameCard.region.pending': 'Pending',
  'gameCard.region.locked': 'Locked',

  /* Linked bundles block */
  /* ── Same-series block (server-side name clustering; hidden when unidentified) ── */
  'gameCard.series.count': '{n} games',
  'gameCard.series.self': 'This game',
  'gameCard.series.openTip': 'View details',

  'gameCard.bundles.title': 'Linked bundles ({n})',
  'gameCard.bundles.completable': 'Can complete',
  'gameCard.bundles.cnPrice': 'CN ¥{amount}',

  /* Third-party stores (CDK price lookup) */
  'gameCard.cdk.title': 'Third-party stores',
  'gameCard.cdk.loading': 'Checking…',
  'gameCard.cdk.notListed': 'Not listed',

  /* Bundle history (Barter.vg count) */
  'gameCard.bundled.tag': 'In {n} bundles',
  'gameCard.bundled.tip': 'This game has appeared in {n} third-party bundles (data source: Barter.vg)',

  /* Gifting analysis popover */
  'gameCard.gift.title': 'Gift region analysis',
  'gameCard.gift.member': 'Member {id}',
  'gameCard.gift.friends': 'Friend gifting ({name} · {region} · base ¥{price})',
  'gameCard.gift.friendsNoPrice': 'Friend gifting ({name} · {region} · no price for this region)',
  'gameCard.gift.noPrimaryPrice':
    'No price data for the primary account region ({region}); friend payments cannot be calculated',
  'gameCard.gift.sendLabel': 'Sends:',
  'gameCard.gift.receiveLabel': 'Receives:',
  'gameCard.gift.sendReceiverPriced':
    '{from} → {to}: {region} price exceeds base ×1.15, pays ¥{amount} ({region} price)',
  'gameCard.gift.sendBase': '{from} → {to}: pays ¥{amount} (base region price)',
  'gameCard.gift.receiveReceiverPriced':
    '{from} → {to}: base price exceeds {region} price ×1.15, they pay ¥{amount} (base price)',
  'gameCard.gift.receiveBase': '{from} → {to}: they pay ¥{amount} ({region} price)',
  'gameCard.gift.globalSend': 'Global gifting (as sender)',
  'gameCard.gift.canSendLabel': 'Can send:',
  'gameCard.gift.cannotSendLabel': 'Cannot send:',
  'gameCard.gift.sendGlobalPriced':
    '{region} price exceeds this region ×1.15, pays ¥{amount} (receiver region price)',
  'gameCard.gift.sendGlobalBase': '{region}: pays ¥{amount} (this region price)',
  'gameCard.gift.globalReceive': 'Global receiving (as receiver)',
  'gameCard.gift.canReceiveLabel': 'Can receive:',
  'gameCard.gift.receiveGlobalPriced':
    '{region}: sender must pay ¥{amount} (this region price exceeds ×1.15)',
  'gameCard.gift.receiveGlobalBase': '{region}: sender pays ¥{amount} ({region} price)',
  'gameCard.gift.rule':
    'Rule: if the receiver region price is ≤ the sender region price ×1.15, pay the sender region price; above that, pay the receiver region price (↗); locked regions cannot receive gifts.',
}

export default gameCard
