/* English 词典 · library（与 zh-CN/library.ts 同构，key 必须逐一对齐）。

   空态两支是**重写**不是直译：中文原句以「」引页面名，英文里换成直白的
   page 名（Tasks / Me），句式也按英文习惯断开。 */

import type { MessageKey } from '../zh-CN'

const library: Partial<Record<MessageKey, string>> = {
  'library.stats': '{total} titles · {loaded} loaded',

  'library.error.title': 'Could not load the library',
  'library.error.network': 'Cannot reach the server',

  /* Empty state A: no games in the catalog (P-M5) — user actions and results
     only; adding shares the dashboard paste flow, bulk import lives on Following */
  /* Empty state B: just added, prices not in yet (freshly imported rows appear
     after the first fetch completes) — the system has it handled, say so */
  'library.empty.library.pending': 'Fetching prices',
  'library.empty.library.pendingHint': 'Added — the game will show up here on its own.',
  'library.empty.library.title': 'No games yet',
  'library.empty.library.hint': 'Add a game and the system will fetch prices for every region automatically.',
  'library.empty.library.paste': 'Paste game links',
  'library.empty.library.import': 'Import a game list',

  /* Empty state C: search/filter active but nothing matched (distinct from “no games”) */
  'library.empty.filter.title': 'No games match these conditions',
  'library.empty.filter.hint': 'Try a different search term, or clear the filters.',
  'library.empty.filter.clear': 'Clear search & filters',

  'library.loadingMore': 'Loading more...',
  'library.end': 'You have reached the end',
}

export default library
