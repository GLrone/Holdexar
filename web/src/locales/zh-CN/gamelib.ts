/* gamelib 词条 —— 游戏库页（views/gamelib/Index.vue 及其页签）。
   页签结构：账号游戏库（每个追踪账户的已购矩阵）/ 库分析 / 家庭库
   （自 family 页迁入，词条仍挂 famLib.* 模块）。

   术语对齐既有 A / B 表：
   · 入库（本系统首次见到该游戏的时间）→ acquired / added
   · 多人共有（追踪账户间）→ co-owned，与家庭库侧的 shared（家庭组共享）
     是两个口径，英文刻意分开
   · 库价值口径 = CN 现价合计，与 famLib.kpi.value 同源 */

const gamelib = {
  /* ── 页签与分节锚点（data-section 值 = 词条 key，HlSectionRail 渲染期 t()）── */
  'gamelib.tab.owned': '账号游戏库',
  'gamelib.tab.insights': '库分析',
  'gamelib.tab.family': '家庭库',
  'gamelib.section.owned': '账号游戏库',
  'gamelib.section.insights': '库分析',
  'gamelib.section.family': '家庭库',

  /* ── 空态与加载 ── */
  'gamelib.empty.loading': '账户已购数据拉取中…',
  'gamelib.empty.error': '拉取失败：{err}',
  'gamelib.empty.noAccounts': '还没有可展示的账户',
  'gamelib.empty.noAccountsHint': '在「我」页绑定 Steam Cookie，或在家庭组添加成员后，这里会汇总每个账号的拥有游戏',
  'gamelib.empty.noGames': '该账号暂无已购游戏数据',
  'gamelib.empty.noGamesHint': '可能是尚未同步，或该账号的已购同步未开启',
  'gamelib.empty.noMatch': '没有符合筛选的游戏',

  /* ── 账号游戏库页签 ── */
  'gamelib.owned.all': '全部账号',
  'gamelib.owned.syncOff': '已购同步未开启',
  'gamelib.owned.kpi.count': '拥有游戏',
  'gamelib.owned.kpi.value': '库价值（CN 现价）',
  'gamelib.owned.kpi.free': '免费游戏',
  'gamelib.owned.kpi.new30': '30天入库',
  'gamelib.owned.kpi.shared': '多人共有',
  'gamelib.owned.searchPlaceholder': '搜索游戏名…',
  'gamelib.owned.sortLabel': '排序：',
  'gamelib.owned.sort.recent': '最近入库',
  'gamelib.owned.sort.name': '名称 A-Z',
  'gamelib.owned.sort.price': '价格',
  'gamelib.owned.sort.owners': '拥有人数',
  'gamelib.owned.filter.shared': '多人共有',

  /* ── 分页 ── */
  'gamelib.pager.range': '第 {from}–{to} 款 · 共 {n} 款',
  'gamelib.pager.refresh': '刷新已购数据',

  /* ── 游戏卡片（components/steamhl/LibGameCard.vue）── */
  'gamelib.card.free': '免费',
  'gamelib.card.noPrice': '暂无价格',

  /* ── 库分析页签 ── */
  'gamelib.insight.accountCmp': '账号对比',
  'gamelib.insight.accountCmp.sub': '各账号拥有款数与库价值（CN 现价合计）',
  'gamelib.insight.genreDist': '类型分布',
  'gamelib.insight.genreDist.sub': '按每款游戏的主类型（第一个标签）统计',
  'gamelib.insight.genreUnknown': '未分类',
  'gamelib.insight.overlap': '账号间重合',
  'gamelib.insight.overlap.sub': '同一款游戏被几个账号同时拥有',
  'gamelib.insight.overlap.exclusive': '单人独享',
  'gamelib.insight.overlap.shared': '多人共有',
  'gamelib.insight.overlap.top': '共有最多的游戏',
  'gamelib.insight.recent': '最近入库',
  'gamelib.insight.recent.sub': '本系统首次见到该游戏的时间（同步入库时刻）',
  'gamelib.insight.topValue': '价值榜',
  'gamelib.insight.topValue.sub': 'CN 现价最高的五款',
  'gamelib.insight.owners': '{n} 人拥有',
  'gamelib.insight.uncrawled': '{n} 款尚未爬取（无价格/封面）',
} as const

export default gamelib
