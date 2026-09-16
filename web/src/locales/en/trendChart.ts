/* English 词典 · trendChart（与 zh-CN/trendChart.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const trendChart: Partial<Record<MessageKey, string>> = {
  'trendChart.empty': 'No price history yet — the chart takes shape as scheduled crawls accumulate.',

  'trendChart.legend.steam': 'Steam price',
  'trendChart.legend.keyStore': 'Key store',
  /* 与 trendDrawer.legend.lowest 是同一条曲线上的同一件东西（zh 都是「历史最低」），
     两处英文必须一致——否则详情页大图说 All-time low、抽屉里说 Lowest。 */
  'trendChart.legend.lowest': 'All-time low',

  /* Range strip (all-time overview × time-window control). 「{n}-month span」写法
     对 n=1 也成立，避免「1 months」这类单复数坑。 */
  'trendChart.thumb.title': 'All-time trend overview',
  'trendChart.thumb.hint': 'Drag the handles to pick the visible range',
  'trendChart.thumb.all': 'All',
  'trendChart.thumb.months': '{n}-month span',
  'trendChart.thumb.range': '{start} → {end} ({span})',

  'trendChart.tip.lowNode': 'new low',
  'trendChart.tip.current': 'Current: ¥{v}',
  'trendChart.tip.currentWithOriginal': 'Current: ¥{v} ({original})',
  'trendChart.tip.discount': '{n}% off',
  'trendChart.tip.keyStore': 'Key store: ¥{v}',
  'trendChart.tip.lowest': 'Lowest: ¥{v}',
}

export default trendChart
