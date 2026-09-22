/** Price event surfaces (game detail "recent price changes" + dashboard cycle digest). */
export default {
  /* Section titles (also used as data-section anchors) */
  'priceEvent.title': 'Recent price changes',
  'priceEvent.empty': 'No price changes in the latest refresh',

  /* Event labels: internal enum values never reach the UI */
  'priceEvent.type.PRICE_DROP': 'Price drop',
  'priceEvent.type.PRICE_INCREASE': 'Price increase',
  'priceEvent.type.NEW_HISTORICAL_LOW': 'New historical low',
  'priceEvent.type.HISTORICAL_LOW_MATCH': 'Matched historical low',
  'priceEvent.type.PERMANENT_PRICE_CHANGE': 'Base price changed',
  'priceEvent.type.REGION_LOCKED': 'Region locked',
  'priceEvent.type.REGION_UNLOCKED': 'Region unlocked',
  'priceEvent.type.PRICE_UNAVAILABLE': 'Not purchasable',
  'priceEvent.type.PRICE_RESTORED': 'Purchasable again',
  'priceEvent.type.FREE_PROMO': 'Free for a limited time',
  'priceEvent.type.REMOVED': 'Removed from store',
  'priceEvent.type.OTHER': 'Price change',

  /* Before / after of a state transition (only on events the backend did detect) */
  'priceEvent.state.ok': 'purchasable',
  'priceEvent.state.locked': 'locked',
  'priceEvent.state.missing': 'pending re-crawl',
  'priceEvent.state.blocked': 'no price',

  /* Relative time (event time comes from the event's own occurred_at) */
  'priceEvent.ago.justNow': 'just now',
  'priceEvent.ago.minutes': '{n} min ago',
  'priceEvent.ago.hours': '{n} h ago',
  'priceEvent.ago.days': '{n} d ago',
  'priceEvent.ago.none': 'time unknown',

  /* Cycle digest (dashboard) */
  'priceEvent.cycle.title': 'This refresh',
  'priceEvent.cycle.empty': 'This refresh produced no price changes',
  'priceEvent.cycle.running': 'Refresh in progress',
  'priceEvent.cycle.finished': 'finished {time}',
  'priceEvent.cycle.item': '{label} × {n}',
  'priceEvent.cycle.eventsTotal': '{n} changes',
  'priceEvent.cycle.games': 'across {n} games',
  'priceEvent.cycle.truncated': 'counted from the latest {n} rows',
  'priceEvent.cycle.more': 'Open library',
} as const
