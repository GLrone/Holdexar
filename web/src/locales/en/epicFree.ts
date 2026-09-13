/* English 词典 · epicFree（与 zh-CN/epicFree.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const epicFree: Partial<Record<MessageKey, string>> = {
  'epicFree.title': 'Epic Free Games',
  'epicFree.updatedAt': 'Updated {time}',
  'epicFree.free': 'FREE',
  'epicFree.live': 'Live',
  'epicFree.upcoming': 'Upcoming',
  'epicFree.endsIn': '{n}d left',
  'epicFree.endsToday': 'Ends today',
  'epicFree.startsIn': 'Starts in {n}d',
  'epicFree.startsTomorrow': 'Starts tomorrow',
  'epicFree.startsToday': 'Starts today',
  'epicFree.startsOn': 'Starts {date}',
  'epicFree.claim': 'Claim now',
  'epicFree.goHub': 'All free games',
  'epicFree.empty': 'No giveaways right now',
  'epicFree.loadFailed': 'Failed to load free games, retrying hourly',
  'epicFree.mobile': 'Mobile',
  'epicFree.mobileCard': 'This week on mobile',
}

export default epicFree
