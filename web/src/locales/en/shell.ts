/* English 词典 · 外壳（与 zh-CN/shell.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const shell: Partial<Record<MessageKey, string>> = {
  'nav.gameDetail': 'Game details',

  'nav.group.more': 'More',
  'nav.group.system': 'System',
  'nav.dashboard': 'Dashboard',
  'nav.library': 'Find Games',
  'nav.gamelib': 'Game Library',
  'nav.achievements': 'Achievements',
  'nav.bundles': 'Bundles',
  'nav.pool': 'Following',
  'nav.family': 'Family',
  'nav.bills': 'Bills',
  'nav.crawl': 'Tasks',
  'nav.proxies': 'Network',
  'nav.alerts': 'Price Alerts',
  'nav.rates': 'Rates',
  'nav.logs': 'Logs',
  'nav.me': 'Settings',
  'nav.about': 'About',
  'nav.toolbox': 'Toolbox',

  'header.crawl': 'Waiting to update',
  'header.crawlRunning': 'Updating game prices',
  'header.crawlDone': 'Prices updated',
  'header.crawlPartial': 'Partly updated',
  'header.crawlTip': 'Some regions are not updated yet; the system will retry automatically',
  'header.crawlTipDone': 'Update finished',

  'wallet.bind': 'Bind wallet',
  'wallet.unboundTip': 'No Steam cookie bound — click to open Settings',
  'wallet.titleMain': 'Primary wallet balance: {balance}',
  'wallet.titleMore': ' (click for all account balances)',
  'wallet.titleSynced': ' · synced at {time}',
  'wallet.popTitle': 'All account balances',
  'wallet.popRefresh': 'Refresh current account balance now',
  'wallet.popFoot': 'Auto refresh every minute · staggered per account',
  'wallet.noNickname': '(no nickname synced)',
  'wallet.badgePrimary': 'Main',
  'wallet.badgeCurrent': 'Active',
  'wallet.toastRefreshed': 'Refreshed: {balance}',
  'wallet.toastFailed': 'Refresh failed, please try again later',

  'avatar.title': 'My Steam account · {name}',
  'avatar.defaultTitle': 'My Steam account',
  'avatar.status': '{title} · Steam status: {status}',
  'avatar.presenceOnline': 'Online',
  'avatar.presenceInGame': 'In game',
  'avatar.presenceOffline': 'Offline',

  'shell.tour': 'Getting started',
  'shell.backtop': 'Back to top',
  'shell.sidebar.expand': 'Expand sidebar',
  'shell.sidebar.collapse': 'Collapse sidebar',
  'shell.theme.toLight': 'Switch to light theme',
  'shell.theme.toDark': 'Switch to dark theme',
  // 语言无关文案：两条都指向**目标语言**，故两种语言下取值相同（见 zh-CN 侧说明）
  'shell.lang.toEn': '切换到 English',
  'shell.lang.toZh': 'Switch to 中文',
  'shell.lang.targetEn': 'EN',
  'shell.lang.targetZh': '中',

  'update.toastAvailable': 'Version v{version} is available — update from the "Me" page',
  'update.navDot': 'Update available',

  'shell.updateReady.title': 'Update downloaded',
  'shell.updateReady.body':
    'v{version} has been downloaded and verified. Restart the app to finish installing.',
  'shell.updateReady.hint':
    'The window closes briefly during the restart and reopens on the new version. Your library and account data are unaffected.',
  'shell.updateReady.restart': 'Restart and update now',
  'shell.updateReady.later': 'Restart later',
}

export default shell
