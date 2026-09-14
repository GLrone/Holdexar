/* gamelib strings — Game Library page (views/gamelib/Index.vue and its tabs).
   Tabs: per-account owned games / insights / family library (moved from the
   family page; its strings still live in the famLib.* module).

   Terminology follows the established A / B tables:
   · 入库 (when this system first saw a game) → acquired / added
   · co-owned = shared between tracked accounts — deliberately distinct from
     shared in the famLib module (family-group sharing semantics)
   · library value = sum of current CN prices, same source as famLib.kpi.value */

const gamelib = {
  /* ── Tabs & section anchors (data-section holds the key; HlSectionRail t()s at render) ── */
  'gamelib.tab.owned': 'Game Library',
  'gamelib.tab.insights': 'Insights',
  'gamelib.tab.family': 'Family Library',
  'gamelib.section.owned': 'Game Library',
  'gamelib.section.insights': 'Insights',
  'gamelib.section.family': 'Family Library',

  /* ── Empty / loading states ── */
  'gamelib.empty.loading': 'Loading owned games…',
  'gamelib.empty.error': 'Failed to load: {err}',
  'gamelib.empty.noAccounts': 'No accounts to show yet',
  'gamelib.empty.noAccountsHint': 'Bind a Steam Cookie on the Me page, or add members in the family group — owned games will show up here',
  'gamelib.empty.noGames': 'No owned games recorded for this account',
  'gamelib.empty.noGamesHint': 'Either not synced yet, or owned-game sync is off for this account',
  'gamelib.empty.noMatch': 'No games match the current filters',

  /* ── Game Library tab ── */
  'gamelib.owned.all': 'All accounts',
  'gamelib.owned.syncOff': 'Owned-game sync is off',
  'gamelib.owned.kpi.count': 'Games Owned',
  'gamelib.owned.kpi.value': 'Library Value (CN price)',
  'gamelib.owned.kpi.free': 'Free Games',
  'gamelib.owned.kpi.new30': 'Added in 30 Days',
  'gamelib.owned.kpi.shared': 'Co-owned',
  'gamelib.owned.searchPlaceholder': 'Search games…',
  'gamelib.owned.sortLabel': 'Sort:',
  'gamelib.owned.sort.recent': 'Recently Added',
  'gamelib.owned.sort.name': 'Name A-Z',
  'gamelib.owned.sort.price': 'Price',
  'gamelib.owned.sort.owners': 'Owners',
  'gamelib.owned.filter.shared': 'Co-owned',

  /* ── Pagination ── */
  'gamelib.pager.range': '{from}–{to} of {n} games',
  'gamelib.pager.refresh': 'Refresh owned games',

  /* ── Game card (components/business/LibGameCard.vue) ── */
  'gamelib.card.free': 'Free',
  'gamelib.card.noPrice': 'No price',

  /* ── Insights tab ── */
  'gamelib.insight.accountCmp': 'By Account',
  'gamelib.insight.accountCmp.sub': 'Games owned and library value per account (sum of CN prices)',
  'gamelib.insight.genreDist': 'Genres',
  'gamelib.insight.genreDist.sub': 'Counted by each game\'s primary (first) genre tag',
  'gamelib.insight.genreUnknown': 'Uncategorized',
  'gamelib.insight.overlap': 'Overlap',
  'gamelib.insight.overlap.sub': 'How many accounts own the same game',
  'gamelib.insight.overlap.exclusive': 'Exclusive to one',
  'gamelib.insight.overlap.shared': 'Shared by many',
  'gamelib.insight.overlap.top': 'Most co-owned games',
  'gamelib.insight.recent': 'Recently Added',
  'gamelib.insight.recent.sub': 'When this system first saw each game (sync time)',
  'gamelib.insight.topValue': 'Top Value',
  'gamelib.insight.topValue.sub': 'The five priciest games at current CN prices',
  'gamelib.insight.owners': '{n} owners',
  'gamelib.insight.uncrawled': '{n} games not crawled yet (no price / cover)',
} as const

export default gamelib
