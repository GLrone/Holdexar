/* English 词典 · 外壳（与 zh-CN/shell.ts 同构，key 必须逐一对齐）。 */

import type { MessageKey } from '../zh-CN'

const shell: Partial<Record<MessageKey, string>> = {
  'app.subtitle': 'Steam multi-region price terminal',

  'nav.gameDetail': 'Game details',

  'nav.group.overview': 'Overview',
  'nav.group.assets': 'Library',
  'nav.group.monitor': 'Monitoring',
  'nav.group.system': 'System',
  'nav.dashboard': 'Dashboard',
  'nav.library': 'Store',
  'nav.gamelib': 'Game Library',
  'nav.bundles': 'Bundles',
  'nav.pool': 'Watch pool',
  'nav.family': 'Family',
  'nav.bills': 'Bills',
  'nav.crawl': 'Tasks',
  'nav.proxies': 'Proxies',
  'nav.alerts': 'Alerts',
  'nav.rates': 'Rates',
  'nav.toolbox': 'Toolbox',
  'nav.logs': 'Logs',
  'nav.me': 'Me',
  'nav.about': 'About',

  'header.crawl': 'Crawl',
  'header.crawlRunning': 'Running {done}/{total} (ok {ok} / fail {fail})',
  'header.crawlIdle': 'Idle',
  'header.crawlTip': 'Speed {speed} t/s · queue {qsize}',

  'wallet.bind': 'Bind wallet',
  'wallet.unboundTip': 'No Steam cookie bound — click to open the profile page',
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
  'shell.sidebar.expandShort': 'Expand',
  'shell.sidebar.collapseShort': 'Collapse sidebar',
  'shell.theme.toLight': 'Switch to light theme',
  'shell.theme.toDark': 'Switch to dark theme',
  // 语言无关文案：两条都指向**目标语言**，故两种语言下取值相同（见 zh-CN 侧说明）
  'shell.lang.toEn': '切换到 English',
  'shell.lang.toZh': 'Switch to 中文',
  'shell.lang.targetEn': 'EN',
  'shell.lang.targetZh': '中',

  'update.toastAvailable': 'Version v{version} is available — update from the "Me" page',
  'update.navDot': 'Update available',
}

export default shell
