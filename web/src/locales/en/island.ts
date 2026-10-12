/* island keys — the dynamic-island message surface (components/ui/HlIsland.vue).
   Holds the island's own fixed actions, task copy, and the idle-state price
   conclusion. The idle price conclusion only occupies the island while the
   data is stale (last successful write over 6h old, or nothing written yet);
   with fresh data and no messages the island retracts entirely. Message bodies
   come from the caller and stay out of the lexicon. */
import type { MessageKey } from '../zh-CN'

const island: Partial<Record<MessageKey, string>> = {
  'island.label': 'Notifications',
  'island.retry': 'Retry',
  'island.view': 'View',
  'island.viewLogs': 'View logs',
  'island.dismiss': 'Dismiss',
  'island.mute': 'Mute these',
  'island.more': '{n} more',
  'island.task.running': 'Fetching prices {done}/{total}',
  'island.task.cancel': 'Cancel task',
  'island.task.open': 'Task center',

  'island.price.waiting': 'Waiting to update',
  'island.price.running': 'Updating game prices',
  'island.price.finalizing': 'Wrapping up this update round…',
  'island.price.done': 'Prices updated',
  'island.price.partial': 'Update incomplete',
  'island.price.stale': 'Prices not updated',
  'island.price.tip': 'Some regions are not updated yet; the system will retry automatically',
  'island.price.tipDone': 'Update finished',

  'island.price.updatedAt': 'Last updated {time}',
  'island.ago.justNow': 'Just now',
  'island.ago.minutes': '{n} min ago',
  'island.ago.hours': '{n} h ago',
  'island.ago.days': '{n} d ago',

  /* Proxy health check / rebuild long task (system-side maintenance, island-wide while running).
     healthProbed counts the L2 layer (node connectivity); healthProbedCount is the
     cumulative fallback when the backend sends no totals (separate keys: the
     placeholder gate requires every call site to supply all placeholders);
     healthExit is the L1 phase (exit identity check, no node count yet). */
  'island.maintenance.health': 'Health check in progress: testing proxy nodes one by one',
  'island.maintenance.healthExit': 'System check in progress: verifying exit identities',
  'island.maintenance.healthProbed': 'System check in progress: {n}/{total} nodes probed',
  'island.maintenance.healthProbedCount': 'System check in progress: {n} nodes probed',
  'island.maintenance.healthDone': 'Health check finished: {ok} healthy · {fail} failed · took {min} min',
  'island.maintenance.healthDoneSimple': 'Health check finished',
  'island.maintenance.rebuild': 'Rebuilding proxy channels',
  'island.maintenance.rebuildDone': 'Proxy channels rebuilt',

  'island.fact.hbChanged': 'Humble Choice monthly bundle refreshed: {label}',
  'island.fact.hbChangedDetail': '{count} games redeemable on Steam this month; see the dashboard card',
  'island.fact.epicRotation': 'Epic free games rotated: {count} new',
  'island.fact.epicRotationDetail': 'Now free to claim: {titles}',
}

export default island
