/* English 词典 · crawl（与 zh-CN/crawl.ts 同构，key 必须逐一对齐）。
   对应源文件：views/crawl/Index.vue。

   措辞对齐（这些词在别处已定，本页沿用）：
   · Wishlist / Owned —— gameCard 与 pool 的既有译法（「愿望单」「已购」）；
   · crawl / sync / bind —— dashboard 与 pool 的既有动词；
   · sale pre-check —— about.source.price.desc 里对「打折预检」的既有说法；
   · friend code —— pool.account.friendCode 的既有说法（本页「好友码」同义）。

   汇总行（progress.meta / import.* 的计数 / jobs.statsText）是参数化整句，
   段间用 · 与 / 分隔——分隔符留在词条内，不在组件侧拼。

   ⚠️ 收藏列表导入的分节说明含两枚 <code>：写进词条值、组件侧 v-html 渲染。
   曾切三段（descPrefix / descMid / descSuffix）并让 descMid 取 ` → `——那是
   被切分逼出来的写法，读成转换关系，原意是嵌套；见 zh-CN/crawl.ts 的说明。 */

import type { MessageKey } from '../zh-CN'

const crawl: Partial<Record<MessageKey, string>> = {
  /* Section anchors (data-section value, shared with the visible heading).
     Watched-region management moved to the Watch pool page (pool.section.regions). */
  'crawl.section.start': 'Start a crawl',
  'crawl.section.bundleImport': 'Bundle import',
  'crawl.section.bulkImport': 'Bulk import',
  'crawl.section.favImport': 'Favorites import',
  'crawl.section.owned': 'Owned games crawl',
  'crawl.section.jobs': 'Job history',

  /* Start a crawl */
  'crawl.start.desc':
    'Regions come from your Me page settings. First-time imports skip the sale pre-check; turn it on for refreshes to save requests.',
  'crawl.start.scopeWishlist': 'Whole watch pool',
  'crawl.start.scopeAppids': 'Specific AppIDs',
  'crawl.start.appidsPlaceholder': 'e.g. 620,105600',
  'crawl.start.button': 'Start task',
  'crawl.start.running': 'Running…',
  'crawl.start.invalidAppids': 'Enter valid AppIDs (comma-separated)',
  'crawl.start.started': 'Job #{id} started ({count} targets)',
  'crawl.stop.button': 'Stop',
  'crawl.stop.requested': 'Stop requested',
  'crawl.stop.none': 'No job is running',
  'crawl.jobs.refresh': 'Refresh job history',

  /* Live progress (SSE) */
  'crawl.progress.meta': 'Done {done} · OK {ok} · Failed {fail} · Queued {qsize} · {speed} t/s',

  /* Auto price chain */
  'crawl.autoPrice.label': 'Auto price updates',
  'crawl.autoPrice.badgeOn': 'Auto on',
  'crawl.autoPrice.badgeOff': 'Manual',
  'crawl.autoPrice.tip':
    'Turn this off to stop scheduled price updates: the 6-hourly refresh (bundles included), failed-price repair and first-crawl of new entries. Board backfill and manual crawls are unaffected (account sync keeps membership in sync only).',
  'crawl.autoPrice.on': 'Auto price updates on: prices are crawled every 6 hours on the anchor grid',
  'crawl.autoPrice.off': 'Auto price updates off: scheduled price refresh stopped — prices update manually',
  'crawl.autoPrice.failed': 'Could not save the setting — please try again',

  /* Bundle import */
  'crawl.bundle.desc':
    'Paste a Steam store / SteamDB bundle or Sub link (or a bare ID). Games inside the bundle join the crawl queue automatically; bundles already in the library are refreshed.',
  'crawl.bundle.import': 'Import',
  'crawl.bundle.refreshed': 'Refreshed: {name}',
  'crawl.bundle.imported': 'Imported: {name}',

  /* Bulk import (joins the watch pool, then a first crawl) */
  'crawl.bulk.desc':
    'Paste Steam store / SteamDB game links or bare AppIDs (separated by spaces, commas or newlines; duplicates are removed). Importing adds the games to the watch pool and starts a first crawl — every game in the pool is crawled for prices; keep it on your Steam wishlist or star it to crawl it first.',
  'crawl.bulk.import': 'Import & monitor',
  'crawl.bulk.moreHidden':
    '{n} more entries omitted — see the Store page for the full list',

  /* Favorites import (FAVORITES channel) — the two <code> tags live inside the
     value and are rendered with v-html (same device as bundles.calc.excludeHint).
     A three-way split was tried first and reverted: pinning both identifiers at
     fixed positions left no room for "the favorites JSON inside FAVORITES_RESPONSE"
     and it came out as "FAVORITES_RESPONSE → favorites" — a transformation, which
     is not what the Chinese says. */
  'crawl.fav.desc':
    'Paste the whole favorites list exported from the browser console (the <code>favorites</code> JSON inside <code>FAVORITES_RESPONSE</code> — an array of IDs or objects). Importing adds the games to the watch pool; games not in the library yet are crawled once as monitoring data.',
  'crawl.fav.import': 'Import favorites',
  'crawl.fav.placeholder':
    '[2561580,1173800,1173820,3837340]\nor [{"appid":620,"name":"Portal 2"},…]',

  /* Shared by both import boxes */
  'crawl.import.importing': 'Importing…',
  'crawl.import.detected': 'Detected {n} AppIDs',
  'crawl.import.detectedInvalid': '{n} unrecognized',
  'crawl.import.added': '{n} newly imported',
  'crawl.import.alreadyTracked': '{n} already in library',
  'crawl.import.poolAdded': '{n} added to the pool',
  'crawl.import.unrecognized': 'Unrecognized {n}',
  'crawl.import.invalid': 'Invalid {n}',
  'crawl.import.firstCrawlStarted': 'First crawl started',
  'crawl.import.noValidAppid': 'No AppID found — could not parse: {list}',
  'crawl.import.noValidAppidMore': 'No AppID found — could not parse: {list} (and more)',
  'crawl.import.noValidItems': 'No valid entries found — could not parse: {list}',
  'crawl.import.noValidItemsMore':
    'No valid entries found — could not parse: {list} (and more)',

  /* Shared actions (select-all moved with the regions section: common.selectAll) */
  'crawl.action.clear': 'Clear',
  'crawl.action.save': 'Save',

  /* Region-count sentence (the owned-games section's "custom" badge).
     The full watched-regions section moved to the Watch pool page (pool.regions.*). */
  'crawl.regions.selected': 'Selected {n} / {total} regions',

  /* Owned-games crawl regions (a subset independent of the watched regions;
     the watched-regions picker itself moved to the Watch pool page) */
  'crawl.owned.desc':
    'Use Account settings to toggle owned-library sync per account (turn it off for very large libraries and watch wishlists only). Owned games join the pool with each wishlist sync; their regions follow the watched regions by default (picked on the Watch pool page), or a subset you choose to save quota.',
  'crawl.owned.acctSet': 'Account settings',
  'crawl.owned.acctSetTip': 'Toggle owned-library sync per account',
  'crawl.owned.follow': 'Follow watched regions',
  'crawl.owned.hintFollow': 'Owned games share the watched regions (picked on the Watch pool page)',
  'crawl.owned.hintCustom': 'Owned games are crawled only in the regions checked below (at least one)',
  'crawl.owned.savedFollow': 'Saved: owned games follow the watched regions',
  'crawl.owned.savedCustom': 'Saved: owned games will be crawled in {n} regions',
  'crawl.owned.emptyWarn':
    'Owned-game regions cannot be empty: check at least one region, or switch back to “Follow watched regions”',

  /* Owned-sync accounts dialog */
  'crawl.acct.title': 'Owned-sync accounts',
  'crawl.acct.hint':
    'On = this account’s owned library joins the watch pool. Turn it off for very large libraries and sync the wishlist only. Changes apply on the next sync.',
  'crawl.acct.meta': 'Friend code {code} · {n} owned',
  'crawl.acct.empty': 'No accounts bound yet — bind one or sync a family group on the Watch pool page.',
  'crawl.acct.ownedOn': 'Owned sync enabled for {name} — applies on the next sync',
  'crawl.acct.ownedOff': 'Owned sync disabled for {name} — applies on the next sync',

  /* Job history table */
  'crawl.jobs.kind': 'Type',
  'crawl.jobs.status': 'Status',
  'crawl.jobs.regions': 'Regions',
  'crawl.jobs.stats': 'Stats',
  'crawl.jobs.startedAt': 'Started',
  'crawl.jobs.statsText': '{ok} OK / {fail} failed / {skip} pre-check skips · {secs}s',

  /* Job statuses */
  'crawl.status.done': 'Done',
  'crawl.status.running': 'Running',
  'crawl.status.failed': 'Failed',
  'crawl.status.stopped': 'Stopped',

  /* Job kinds (unknown kinds fall back to the raw value) */
  'crawl.kind.manual': 'Manual',
  'crawl.kind.scheduled': 'Scheduled',
  'crawl.kind.wishlistSync': 'Wishlist sync',
  'crawl.kind.ownedSync': 'Owned sync',
  'crawl.kind.missing': 'Missing backfill',
  'crawl.kind.repair': 'Failure repair',
  'crawl.kind.backfill': 'Orphan backfill',
  'crawl.kind.import': 'Bulk import',
  'crawl.kind.favImport': 'Favorites import',
  'crawl.kind.poolAdd': 'Pool add',
}

export default crawl
