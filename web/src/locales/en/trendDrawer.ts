/* English 词典 · trendDrawer（与 zh-CN/trendDrawer.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const trendDrawer: Partial<Record<MessageKey, string>> = {
  /* Time-range chips */
  'trendDrawer.range.90d': '90 days',
  'trendDrawer.range.1y': '1 year',
  'trendDrawer.range.3y': '3 years',
  'trendDrawer.range.all': 'All time',

  /* Version labels */
  'trendDrawer.version.standard': 'Standard',
  'trendDrawer.version.gold': 'Gold Edition',

  /* All-time-low event markers */
  'trendDrawer.event.first': 'First',
  'trendDrawer.event.lowest': 'Lowest',
  'trendDrawer.event.drop': 'Drop',

  /* Chart card header and price state */
  'trendDrawer.header.subtitle': 'AppID {appid} · {region} price trend',
  'trendDrawer.price.discount': '▼ -{pct}% · On sale',
  'trendDrawer.price.noDiscount': 'Full price',

  /* Legend and stat labels */
  'trendDrawer.legend.localPrice': 'Local price',
  'trendDrawer.legend.lowest': 'All-time low',
  'trendDrawer.stats.current': 'Current',
  'trendDrawer.stats.highest': 'Range high',
  /* lowestHits = 「追平或跌破史低」的事件数，同一促销期内连续快照只计一次
     （api/client.ts:437）。原译 'Low hits' 不是英语说法。 */
  'trendDrawer.stats.lowestHits': 'Times at low',
  'trendDrawer.stats.slices': 'Data points',

  /* Empty state */
  'trendDrawer.empty.noData': 'No price history for this version in this region',
  'trendDrawer.empty.hint': 'The chart will take shape as scheduled crawls accumulate.',

  /* All-versions block */
  'trendDrawer.versions.title': 'All versions',
  'trendDrawer.versions.hint': '(current region price · click to switch)',

  /* Drawer footer link (the → arrow stays in the component) */
  'trendDrawer.detail.viewFull': 'View full details',
}

export default trendDrawer
