/* English 词典 · dashboard（与 zh-CN/dashboard.ts 同构，key 必须逐一对齐）。

   角标（badge.*）是固定宽度的短标签，一律取最短的英语说法：
   「新史低」= New low（不是 "New all-time low"），「永降」= Price cut。 */

import type { MessageKey } from '../zh-CN'

const dashboard: Partial<Record<MessageKey, string>> = {
  /* Section anchors + card titles */
  'dashboard.section.overview': 'Overview',
  'dashboard.section.priceDrops': 'Price drops',
  'dashboard.section.epicFree': 'Epic Free Games',
  'dashboard.section.spotlight': 'New-low picks',
  'dashboard.section.priceMoves': 'Price activity',
  'dashboard.section.rates': 'Key rates',
  'dashboard.section.quickActions': 'Quick actions',
  'dashboard.section.sysInfo': 'System info',

  /* Stat tiles */
  'dashboard.stats.totalGames': 'Titles in the store',
  'dashboard.stats.discounts': 'On sale now',
  'dashboard.stats.monitored': 'Monitored games',
  'dashboard.stats.proxyAvailable': 'Proxies up',

  /* Uptime (three granularities) */
  'dashboard.uptime.hms': '{h}h {m}m {s}s',
  'dashboard.uptime.ms': '{m}m {s}s',
  'dashboard.uptime.s': '{s}s',

  /* Proxy stat sub-line (independent phrases joined by ·) */
  'dashboard.proxy.clashRunning': 'Clash: {n} exits',
  'dashboard.proxy.clashStopped': 'Clash not running',
  'dashboard.proxy.pool': 'Pool {ok}/{total}',

  /* Price-state badges (marquee / carousel / activity list) */
  'dashboard.badge.newLow': 'New low',
  'dashboard.badge.tieLow': 'Tied low',
  'dashboard.badge.discount': 'On sale',
  'dashboard.badge.permDrop': 'Price cut',

  /* Alt text for the 🏆 cheapest-region icon */
  'dashboard.lowestRegion': 'Cheapest region',

  /* Card-corner link (the → arrow stays in the component) */
  'dashboard.action.viewAll': 'All',

  /* Quick-action buttons */
  'dashboard.action.crawl': 'Start crawl',
  'dashboard.action.syncWishlist': 'Sync watch pool',
  'dashboard.action.refreshRates': 'Refresh rates',
  'dashboard.action.checkProxies': 'Check proxies',

  /* Quick-action result toasts */
  'dashboard.toast.crawlStarted': 'Crawl job #{id} started ({count} games)',
  'dashboard.toast.noAccounts': 'No tracking account bound — bind one on the Watch pool page first',
  'dashboard.toast.synced': 'Synced {ok}/{total} accounts, {added} new games',
  'dashboard.toast.crawlTriggerFailed': 'Could not start a crawl for the new items (a job may already be running) — try again later',
  'dashboard.toast.ratesRefreshed': 'Rates refreshed: {count} currencies (source: {source})',
  'dashboard.toast.proxyCheckStarted': 'Proxy health check started ({state})',

  /* Empty states */
  'dashboard.empty.noMoves': 'No price activity yet',
  'dashboard.empty.noRates': 'No rate data yet — pick currencies to track on the Rates page',

  /* System-info rows */
  'dashboard.sys.app': 'App',
  'dashboard.sys.version': 'Version',
  'dashboard.sys.uptime': 'Uptime',
}

export default dashboard
