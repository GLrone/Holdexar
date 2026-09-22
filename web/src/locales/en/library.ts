/* English 词典 · library（与 zh-CN/library.ts 同构，key 必须逐一对齐）。

   空态两支是**重写**不是直译：中文原句以「」引页面名，英文里换成直白的
   page 名（Tasks / Me），句式也按英文习惯断开。 */

import type { MessageKey } from '../zh-CN'

const library: Partial<Record<MessageKey, string>> = {
  'library.stats': '{total} titles · {loaded} loaded',

  'library.error.title': 'Could not load the library',
  'library.error.network': 'Cannot reach the server',

  'library.empty.library.title': 'The store is still empty',
  /* Network hint: shown only when games were added but no data arrives.
     A proxy is an improvement, not a prerequisite (direct connection is the
     standard) — keep the wording hedged ("may", "usually"), no hard causality. */
  'library.empty.library.netHint':
    'No data for a long time? The current network may be struggling to reach Steam — setting up a proxy usually helps.',
  'library.empty.library.goProxy': 'Set up a proxy',
  /* Empty watch pool: one-tap jump to manual import */
  'library.empty.library.goImport': 'Add games',
  'library.empty.library.pool':
    'The watch pool holds {n} games, but no price data yet.',
  'library.empty.library.crawling':
    'Prices are updating — cards will show up here once it finishes.',
  'library.empty.library.startHint':
    'Prices refresh on a fixed schedule; you can also trigger one update from the Tasks page.',
  'library.empty.library.noPool1':
    'Start by adding the games you want to compare:',
  'library.empty.library.noPool2':
    'Paste game links on the Tasks page; with a bound Steam account, your wishlist and owned games join automatically.',
  'library.empty.library.checking': 'Checking the watch pool…',

  'library.empty.filter.title': 'No matches',
  'library.empty.filter.hint': 'Try a different search or price filter.',

  'library.loadingMore': 'Loading more...',
  'library.end': 'You have reached the end',
}

export default library
