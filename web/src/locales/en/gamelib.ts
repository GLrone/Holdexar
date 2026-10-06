/* English 词典 · gamelib —— views/gamelib/Index.vue and its tabs: per-account owned games / insights /
   family library（词条在 famLib.*） / play activity（词条在 famPlay.*）。与 zh-CN/gamelib.ts 同构，key 必须逐一对齐。
   措辞沿用既有译法：acquired / added（首次见到该游戏的时间）；co-owned 与 famLib 的 shared 刻意分词（两个口径）；
   library value = CN 现价合计，与 famLib.kpi.value 同源。 */

const gamelib = {
  /* ── Tabs & section anchors (data-section holds the key; HlSectionRail t()s at render) ── */
  'gamelib.tab.owned': 'Game Library',
  'gamelib.tab.insights': 'Insights',
  'gamelib.tab.family': 'Family Library',
  'gamelib.tab.play': 'Play activity',
  'gamelib.section.owned': 'Game Library',
  'gamelib.section.insights': 'Insights',
  'gamelib.section.family': 'Family Library',
  'gamelib.section.play': 'Play activity',

  /* ── Empty / loading states ── */
  'gamelib.empty.loading': 'Loading owned games…',
  'gamelib.empty.error': 'Failed to load: {err}',
  'gamelib.empty.noAccounts': 'No accounts to show yet',
  'gamelib.empty.noAccountsHint': 'Bind a Steam Cookie on the Settings page, or add members in the family group — owned games will show up here',
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
  'gamelib.insight.tagDist': 'Tags',
  'gamelib.insight.tagDist.sub': 'Counted by each game\'s top popular tag',
  'gamelib.insight.tagUnknown': 'Uncategorized',
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
