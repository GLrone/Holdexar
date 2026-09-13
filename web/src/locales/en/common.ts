/* English 词典 · 通用（与 zh-CN/common.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const common: Partial<Record<MessageKey, string>> = {
  'common.empty': 'No data',
  'common.loading': 'Loading…',
  'common.confirm': 'Confirm',
  'common.cancel': 'Cancel',
  'common.close': 'Close',
  'common.retry': 'Retry',
  'common.refresh': 'Refresh',
  'common.copy': 'Copy',
  'common.copied': 'Copied',
  'common.all': 'All',
  'common.total': 'Total',

  /* Shared actions (watch-regions section buttons etc.) */
  'common.save': 'Save',
  'common.selectAll': 'Select all',
  'common.clear': 'Clear',

  'common.select': 'Select',
  'common.selectDate': 'Select date',
  'common.confirmTip': 'Run this action?',

  'common.prev': 'Back',
  'common.next': 'Next',
  'common.finish': 'Finish',
  'common.restart': 'Start over',
  'stepper.doneTitle': 'All set 🎉',
  'stepper.doneHint': 'The tour is complete — you can start it over anytime.',

  /* Playtime units — shared, so they live here rather than in either family tab.
     "k" is kept because these sit in KPI cards where the number must stay short. */
  'common.hours': '{h} hrs',
  'common.hoursK': '{h}k hrs',

  /* Suffix for the narrow-column region abbreviation (2nd arg of
     `compactRegionName` in api/regions.ts). The Chinese rule takes the first
     two characters and appends 区 — "哈萨克斯坦" → "哈萨区". Applying that to
     English yields "Ka区", so the suffix is locale-supplied instead: 区 in
     Chinese, and deliberately the EMPTY STRING here. Empty is a meaningful
     value for this outlet, not a missing translation: it means "do not
     abbreviate, return the name as-is", leaving truncation to CSS ellipsis
     ("Kazakh…" reads far better than "Ka区"). */
  'common.regionSuffix': '',

  'rail.unnamed': 'Section',
  'rail.navLabel': 'Page section navigation',
  'rail.jumpTo': 'Jump to “{label}”',

  'stub.title': 'Module under construction',
  'stub.hint':
    'The route is mounted but its view is not finished yet (parallel work in progress). This placeholder disappears once the real page lands.',
}

export default common
