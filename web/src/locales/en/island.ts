/* island keys — the dynamic-island message surface (components/ui/HlIsland.vue).
   Holds the island's own fixed actions, task copy, and the idle-state price
   conclusion. Message bodies come from the caller and stay out of the lexicon. */
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
  'island.price.done': 'Prices updated',
  'island.price.partial': 'Update incomplete',
  'island.price.tip': 'Some regions are not updated yet; the system will retry automatically',
  'island.price.tipDone': 'Update finished',

  'island.price.updatedAt': 'Last updated {time}',
  'island.ago.justNow': 'Just now',
  'island.ago.minutes': '{n} min ago',
  'island.ago.hours': '{n} h ago',
  'island.ago.days': '{n} d ago',

  'island.fact.hbChanged': 'Humble Choice monthly bundle refreshed: {label}',
  'island.fact.hbChangedDetail': '{count} games redeemable on Steam this month; see the dashboard card',
  'island.fact.epicRotation': 'Epic free games rotated: {count} new',
  'island.fact.epicRotationDetail': 'Now free to claim: {titles}',
}

export default island
