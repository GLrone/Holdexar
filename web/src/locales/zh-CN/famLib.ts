/* famLib 词条 —— 游戏库·家庭库页签（views/gamelib/tabs/FamilyLib.vue）。
   不变量：「近30日活跃」词条统计 timeAcquired，英文必须是 "Acquired in last 30 days"、禁用 active；
   时长单位 h / kh 不在本模块（非语言中立），落 common.hours / common.hoursK；
   famLib.pager.range 的 <b> 写在值里由组件侧 v-html 渲染，强调色留在 CSS；
   术语（未收录 / 独占 / 多人共享等）英文对齐既有译法，跨模块不另起一套。 */

const famLib = {
  /* ── 空态与引导 ── */
  'famLib.empty.bindHint': '绑定后点家庭页顶部「⟳ 同步家庭组」，家庭库将自动聚合共享清单与成员已购',
  'famLib.empty.loading': '家庭库数据拉取中…',
  'famLib.empty.noData': '暂无家庭库数据',
  'famLib.empty.noDataHint': '加入 Steam 家庭组并点「⟳ 同步家庭组」后，此处展示共享库 ∪ 成员已购',

  /* ── 6 张 KPI 卡 ── */
  'famLib.kpi.total': '家庭库游戏总数',
  'famLib.kpi.exclusive': '独占贡献',
  'famLib.kpi.shared': '多人共享',
  'famLib.kpi.active30': '近30日活跃',
  'famLib.kpi.playtime': '总游玩时长',
  'famLib.kpi.value': '库总价值（CN 现价）',

  /* ── 排序工具栏 ── */
  'famLib.toolbar.searchPlaceholder': '搜索家庭库游戏…',
  'famLib.toolbar.sortLabel': '排序：',
  'famLib.toolbar.onlyExclusive': '仅独占',
  'famLib.sort.name': '名称 A-Z',
  'famLib.sort.playtime': '游玩时长',
  'famLib.sort.price': '价格',

  /* 排序「最近游玩」与卡片里那一行是同一个标签，共用一条，改一处两处同步 */
  'famLib.label.lastPlayed': '最近游玩',

  /* ── 卡片 ── */
  'famLib.card.notListed': '未收录',
  'famLib.card.free': '免费',
  'famLib.card.exclusive': '独占',
  'famLib.card.sharedBy': '{n}人共享',
  'famLib.card.playtime': '时长',
  'famLib.card.cnPrice': '国区现价',

  /* ── 网格与分页 ── */
  'famLib.grid.noMatch': '没有匹配的家庭库游戏',
  'famLib.pager.range': '显示 {from}-{to} / 共 <b>{n}</b> 款游戏',
  'famLib.pager.page': '第 {page} / {total} 页',
  'famLib.pager.prev': '上一页',
  'famLib.pager.next': '下一页',

  /* ⟳ 留在组件侧（纯符号），词条只收它后面的词 */
  'famLib.action.refresh': '刷新家庭库（重拉 Steam）',
} as const

export default famLib
