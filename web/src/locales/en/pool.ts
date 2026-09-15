/* English 词典 · pool（与 zh-CN/pool.ts 同构，key 必须逐一对齐）。
   对应源文件：views/pool/Index.vue。

   措辞对齐（这些词的译法在别处已定，本页沿用）：
   · Wishlist / Owned —— gameCard 的归属徽章（gameCard.status.wishlist / .owned），
     也是账户 kinds 维度的原文概念；
   · bind / unbind —— dashboard 页指向本页的引导用词；
   · Sync / crawl —— dashboard 与 shell 的既有译法；
   · “识别到 N 个 AppID”复用 crawl.import.detected / .detectedInvalid——
     同一份粘贴解析在任务页与池页逐字相同。

   三段同步结果文案各自成条，不在组件侧拼片段（中文那个「，已自动开始爬取」
   是随条件增减的整句，直译成英文会拼不出版行）。 */

import type { MessageKey } from '../zh-CN'

const pool: Partial<Record<MessageKey, string>> = {
  /* Section anchors (data-section value, shared with the visible heading) */
  'pool.section.steamAccount': 'Steam accounts',
  'pool.section.regions': 'Watched regions',
  'pool.section.items': 'Tracked items',

  /* Steam accounts section */
  'pool.account.desc':
    'Binding syncs the account\u2019s wishlist and owned games into the watch pool (the profile must be public) — the data source for crawls.',
  'pool.account.steamidPlaceholder': 'Steam friend code / SteamID64 / profile URL',
  'pool.account.labelPlaceholder': 'Label (optional)',
  'pool.account.bind': 'Bind',
  'pool.account.bindSuccess': 'Account bound: {code}',
  'pool.account.unbindTitle': 'Confirm',
  'pool.account.unbindConfirm': 'Unbind {name}? Their tracked items will be deleted too.',
  'pool.account.unbindSuccess': 'Account unbound',
  'pool.account.sync': 'Sync',
  'pool.account.syncAdded': 'Sync complete: {n} new games',
  'pool.account.syncAddedCrawling': 'Sync complete: {n} new games, crawl started automatically',
  'pool.account.syncNoNew': 'Sync complete: {n} items in the pool, nothing new',
  'pool.account.neverSynced': 'Never synced',
  'pool.account.friendCode': 'Friend code {code}',
  'pool.account.kindWishlist': 'Wishlist',
  'pool.account.kindOwned': 'Owned',
  'pool.account.colAccount': 'Account',
  'pool.account.colItemCount': 'Tracked items',
  'pool.account.colLastSync': 'Last sync',
  'pool.account.colActions': 'Actions',

  /* Watched regions section */
  'pool.regions.desc':
    'Crawls run only in the regions you check — unchecked regions are never crawled. Clearing every region makes crawl jobs refuse to start.',
  'pool.regions.selected': 'Selected {n} / {total} regions',
  'pool.regions.savedNone': 'Saved: no regions enabled — crawl jobs will refuse to start',
  'pool.regions.savedStrict': 'Saved: crawls will run strictly in the enabled regions',
  'pool.regions.searchPlaceholder': 'Search region name or code',
  'pool.regions.noMatch': 'No region matches',

  /* Tracked items section header */
  'pool.items.matchCount': 'Matching {matched} of {total} items',
  'pool.items.totalHint': '{total} items — click for price details.',
  'pool.items.searchPlaceholder': 'Search by game name or AppID',
  'pool.items.crawlAll': 'Crawl the whole pool',

  /* Pool management (add / remove / bulk operations) */
  'pool.items.kind.all': 'All',
  'pool.items.kind.follow': 'Followed',
  'pool.items.kind.wishlist': 'Wishlist',
  'pool.items.kind.owned': 'Owned',
  'pool.items.kind.board': 'Trending',
  'pool.items.kind.manual': 'Manual',
  'pool.items.tipAccounts': 'Tracked by {n} accounts',
  'pool.items.add': 'Add items',
  'pool.items.addTitle': 'Add tracked items',
  'pool.items.addHint':
    'Every item added to the pool is crawled for prices; games on your Steam wishlist or starred as followed are crawled first. Paste or use "Choose file" (JSON array / links / raw AppIDs all work) — AppIDs imported from a file also join the preset game pool shipped with the app seed.',
  'pool.items.addPlaceholder':
    'Paste Steam store / SteamDB links or raw AppIDs (spaces, commas and new lines all work)',
  'pool.items.importFile': 'Choose file',
  'pool.items.importFileLoaded': 'Loaded {name}',
  'pool.items.addSubmit': 'Add to pool',
  'pool.items.addNoValid': 'No valid AppID found',
  'pool.items.addResult':
    'Added to the pool: {added} new · {restored} restored · {exists} already in pool · {invalid} unrecognized',
  'pool.items.manage': 'Manage',
  'pool.items.manageDone': 'Done',
  'pool.items.manageHint': 'Select items and remove them in bulk; removed items are no longer crawled.',
  'pool.items.selected': '{n} selected',
  'pool.items.selectPage': 'Select page',
  'pool.items.unselectPage': 'Unselect page',
  'pool.items.clearSelection': 'Clear selection',
  'pool.items.removeSelected': 'Remove selected',
  'pool.items.removeConfirm': 'Remove the {n} selected games from the pool?',
  'pool.items.removeResult': '{n} games removed from the pool',

  /* Item grid and empty states */
  'pool.items.pendingName': 'Not fetched yet (new entry)',
  'pool.items.noMatch': 'No match for “{query}” — not in the tracked items.',
  'pool.items.noKindMatch': 'No items in this category.',
  'pool.items.empty': 'No tracked items yet — bind an account and sync to get started.',
}

export default pool
