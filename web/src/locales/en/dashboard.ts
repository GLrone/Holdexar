/* English 词典 · dashboard（与 zh-CN/dashboard.ts 同构，key 必须逐一对齐）。

   角标（badge.*）是固定宽度的短标签，一律取最短的英语说法：
   「新史低」= New low（不是 "New all-time low"），「永降」= Price cut。 */

import type { MessageKey } from '../zh-CN'

const dashboard: Partial<Record<MessageKey, string>> = {
  /* Section anchors + card titles */
  'dashboard.section.overview': 'Overview',
  'dashboard.section.steamFree': 'Steam free games',
  'dashboard.section.priceDrops': 'Price drops',
  'dashboard.section.epicFree': 'Epic Free Games',
  'dashboard.section.hbChoice': 'Humble Choice',
  'dashboard.section.spotlight': 'New-low picks',
  'dashboard.section.priceMoves': 'Price activity',
  'dashboard.section.rates': 'Key rates',

  /* Stat tiles (the whole row is hidden while the store is empty) */
  'dashboard.stats.totalGames': 'Titles in the store',
  'dashboard.stats.discounts': 'On sale now',
  'dashboard.stats.monitored': 'Monitored games',

  /* Empty-store first screen: answers "what do I do first" */
  'dashboard.welcome.title': 'Welcome to {app}',
  'dashboard.welcome.ask': 'Which games do you want to watch?',
  'dashboard.welcome.paste': 'Paste a game link',
  'dashboard.welcome.sync': 'Sync from Steam wishlist',
  'dashboard.welcome.import': 'Import a game list / file',
  'dashboard.welcome.pasteHint':
    'One link per line (Steam store / SteamDB link or plain AppID) — you can paste a whole list at once.',
  'dashboard.welcome.add': 'Add',
  'dashboard.welcome.adding': 'Adding…',
  'dashboard.welcome.added': 'Added {n} — fetching prices for every region',
  'dashboard.welcome.addOwned': '{n} already in your store',
  'dashboard.welcome.addNone': 'No usable game found — check the links or AppIDs',
  'dashboard.welcome.addFailed': 'Could not add right now — please try again.',
  'dashboard.welcome.autoTitle': 'It will automatically',
  'dashboard.welcome.autoPrice': 'fetch regional prices',
  'dashboard.welcome.autoRefresh': 'refresh on schedule',
  'dashboard.welcome.autoEvent': 'spot price drops',
  'dashboard.welcome.autoAlert': 'alert you on target',

  /* Price-state badges (marquee / carousel / activity list) */
  'dashboard.badge.newLow': 'New low',
  'dashboard.badge.tieLow': 'Tied low',
  'dashboard.badge.discount': 'On sale',
  'dashboard.badge.permDrop': 'Price cut',

  /* Alt text for the 🏆 cheapest-region icon */
  'dashboard.lowestRegion': 'Cheapest region',

  /* Card-corner link (the → arrow stays in the component) */
  'dashboard.action.viewAll': 'All',

  /* Wishlist-sync result toasts */
  'dashboard.toast.noAccounts': 'No Steam account yet — bind one to sync your wishlist',
  'dashboard.toast.synced': 'Synced {ok}/{total} accounts, {added} new games',
  'dashboard.toast.crawlTriggerFailed': 'Could not start price fetching for the new items (a job may already be running) — it will retry later',

  /* Empty states */
  'dashboard.empty.noMoves': 'No price activity yet',
  'dashboard.empty.noRates': 'No rate data yet — pick currencies to track on the Rates page',
}

export default dashboard