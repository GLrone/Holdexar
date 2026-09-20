/* English 词典 · steamFree（与 zh-CN/steamFree.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const steamFree: Partial<Record<MessageKey, string>> = {
  'steamFree.title': 'Steam Giveaways',
  'steamFree.updatedAt': 'Updated {time}',
  'steamFree.free': 'FREE',
  'steamFree.live': 'Live',
  'steamFree.endsInDays': '{n}d left',
  'steamFree.endsInHours': '{n}h left',
  'steamFree.endsToday': 'Ends today',
}

export default steamFree
