/* English 词典 · library（与 zh-CN/library.ts 同构，key 必须逐一对齐）。

   空态两支是**重写**不是直译：中文原句以「」引页面名，英文里换成直白的
   page 名（Tasks / Me），句式也按英文习惯断开。 */

import type { MessageKey } from '../zh-CN'

const library: Partial<Record<MessageKey, string>> = {
  'library.stats': '{total} titles · {loaded} loaded',

  'library.error.title': 'Could not load the library',
  'library.error.network': 'Cannot reach the server',

  'library.empty.library.title': 'The store is still empty',
  'library.empty.library.pool':
    'The watch pool holds {n} games, but no price data has been crawled yet.',
  'library.empty.library.crawling': 'A crawl is running — results will show up here once it finishes.',
  'library.empty.library.startHint':
    'Start a crawl from the Tasks page, or wait for the scheduled job to run.',
  'library.empty.library.noPool1':
    'The watch pool has no games yet. Add AppIDs under Tasks → Bulk import,',
  'library.empty.library.noPool2':
    'or bind a Steam account on the Watch pool page and sync — the crawler picks those up automatically.',
  'library.empty.library.checking': 'Checking the watch pool…',

  'library.empty.filter.title': 'No matches',
  'library.empty.filter.hint': 'Try a different search or price filter.',

  'library.loadingMore': 'Loading more...',
  'library.end': 'You have reached the end',
}

export default library
