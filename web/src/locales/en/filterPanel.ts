/* English 词典 · filterPanel（与 zh-CN/filterPanel.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const filterPanel: Partial<Record<MessageKey, string>> = {
  'filterPanel.advanced.title': '⚙ Advanced filters',

  'filterPanel.section.basic': 'Basic filters',
  'filterPanel.section.lowest': 'Lowest price status',
  'filterPanel.section.platform': 'Platform availability',
  'filterPanel.section.priceRange': 'Price range (selected region)',
  'filterPanel.section.diff': 'Price diff vs. CN (selected region)',
  'filterPanel.section.reviews': 'Review count range',
  'filterPanel.section.rating': 'Positive rating range',
  'filterPanel.section.other': 'Other',

  'filterPanel.basic.top3': 'Show top 3 cheapest regions',
  'filterPanel.basic.top3ListHint': '(list view shows lowest price only)',
  'filterPanel.basic.strictLowest': 'Absolute lowest',
  'filterPanel.basic.tolerance': 'Diff tolerance',
  'filterPanel.basic.toleranceUnit': 'CNY',
  'filterPanel.basic.toleranceHint': '(0 means absolute lowest)',

  'filterPanel.lowest.new': 'New low',
  'filterPanel.lowest.equal': 'Matches low',
  'filterPanel.lowest.non': 'Not at low',

  'filterPanel.platform.epic': 'Epic giveaway',
  'filterPanel.platform.hb': 'Humble Bundle',
  'filterPanel.platform.xgp': 'Xbox Game Pass',

  'filterPanel.diff.giftOnly': 'Gift only',
  'filterPanel.diff.absolute': 'Absolute',
  'filterPanel.diff.percent': 'Percent',
  'filterPanel.diff.minPlaceholder': 'Min diff',
  'filterPanel.diff.maxPlaceholder': 'Max diff',

  'filterPanel.reviews.unit': 'reviews',

  'filterPanel.other.onlyDiscounted': 'Discounted only',
  'filterPanel.other.hideOwned': 'Hide owned',
  'filterPanel.other.hideDlc': 'Hide DLC',

  /* 中文侧的半角空格是字间距 hack（见 zh-CN 侧说明），英文侧正常书写 */
  'filterPanel.action.reset': 'Reset',
  'filterPanel.action.apply': 'Apply',
}

export default filterPanel
