/* English 词典 · navbar（与 zh-CN/navbar.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const navbar: Partial<Record<MessageKey, string>> = {
  /* Sort dropdown */
  'navbar.sort.default': '⭐ Default',
  'navbar.sort.smart': '🧠 Smart',
  'navbar.sort.rating': '👍 Top rated',
  'navbar.sort.priceDiff': '💰 Biggest gap',
  'navbar.sort.discount': '🏷️ Deepest discount',
  'navbar.sort.top100': '🔥 Recent Top 100',
  'navbar.sort.new2026': '📅 Releasing in 2026',

  /* Current region shown on the dropdown button */
  'navbar.region.allLowest': 'Lowest worldwide',
  'navbar.region.locked': 'Region-locked',

  /* Region dropdown options */
  'navbar.regionOption.allLowest': 'Lowest worldwide (auto FX)',
  'navbar.regionOption.cn': 'China',
  'navbar.regionOption.locked': '🔒 Region-locked games',

  /* Search box */
  'navbar.search.placeholder': 'Search games… (Enter)',

  /* Layout toggle */
  'navbar.layout.grid': 'Grid view',
  'navbar.layout.list': 'List view',

  /* Advanced filter entry */
  'navbar.advancedFilter': '⚙️ Advanced filters',

  /* Region filter toolbar: modes + clear */
  'navbar.filterMode.global': '👑 Lowest worldwide',
  'navbar.filterMode.cheaper': '📉 Cheaper than China',
  'navbar.filterMode.highDiff': '💰 Big potential gap',
  'navbar.filter.clear': '✕ Clear',
}

export default navbar
