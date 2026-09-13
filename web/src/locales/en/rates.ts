/* English 词典 · rates（与 zh-CN/rates.ts 同构，key 必须逐一对齐）。

   `{currency}` / `{code}` 双占位符：中文条目用 {currency}（「美元 历史走势」），
   英文条目用 {code}（「USD history」）——词汇来源 api/currencies.ts 只有中文名，
   英文侧改用 ISO 代号，两边都读得通（详见 zh-CN/rates.ts 文件头）。 */

import type { MessageKey } from '../zh-CN'

const rates: Partial<Record<MessageKey, string>> = {
  /* Section anchors + card titles */
  'rates.section.rates': 'Rates (vs CNY)',
  'rates.section.history': 'History',
  'rates.section.convert': 'Converter',
  'rates.section.allCurrencies': 'All currencies',

  /* Page-header description (the "last source" tail is a separate full sentence) */
  'rates.header.desc':
    'Basis for all price comparisons; the server fetches every region currency daily — pick which ones to track here.',
  'rates.header.descWithSource':
    'Basis for all price comparisons; the server fetches every region currency daily — pick which ones to track here (last source: {source}).',

  'rates.action.refresh': 'Refresh now',

  /* Tracked-currency picker */
  'rates.tracked.label': 'Tracked currencies',
  'rates.tracked.placeholder': 'Select currencies to track',
  'rates.tracked.empty': 'No currencies tracked — select some under "Tracked currencies" above.',

  /* History */
  'rates.history.title': '{code} history',
  'rates.history.empty': 'No {code} history yet — it builds up as rates refresh daily.',

  /* Time-range chips (mapped from RATE_RANGES ids) */
  'rates.range.oneMonth': '1M',
  'rates.range.sixMonths': '6M',
  'rates.range.oneYear': '1Y',
  'rates.range.fiveYears': '5Y',
  'rates.range.tenYears': '10Y',
  'rates.range.all': 'All',

  /* echarts axis name and hover tooltip (formatter builds HTML) */
  'rates.chart.yAxis': '{code} → CNY',
  'rates.chart.tooltip': '{date}<br/>{code} → CNY: <b style="color:{color}">{value}</b>',

  /* Currency converter */
  'rates.conv.hint': '1 {from} ≈ {rate} {to}',
  'rates.conv.amountPlaceholder': 'Enter amount',
  'rates.conv.swap': 'Swap direction',

  /* All-currencies table */
  'rates.allCurrencies.title': 'All currencies ({n})',
  'rates.table.name': 'Name',
  'rates.table.currency': 'Code',
  'rates.table.rate': 'vs CNY',
  'rates.table.updated': 'Updated',

  /* Refresh result toast */
  'rates.toast.refreshed': 'Refreshed {count} currencies (source: {source})',
}

export default rates
