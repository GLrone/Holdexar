/* famBuy 词条 —— 家庭组·购买动态页签（views/family/tabs/FamBuy.vue）。
   不变量：购入时间 → Acquired（同一字段 rt_time_acquired，禁写成 Date）；购买者 → Buyer；拥有者 → Owners；快照数据 → Snapshot；
   famBuy.toolbar.count 的 <b> 写在值里由组件侧 v-html 渲染（强调色与等宽字留在 CSS :deep()，样式不进词典）；
   {n}人 单独成条（英文不需要该量词，值就是 {n}）。 */

const famBuy = {
  /* ── 成员筛选（HlSelect 选项）── */
  'famBuy.member.all': '全部购买者',
  'famBuy.member.fallback': '成员{id}',

  'famBuy.card.free': '免费',

  /* ── 空态 ── */
  'famBuy.empty.loading': '家庭库数据拉取中…',
  'famBuy.empty.noData': '暂无入库时间数据——同步家庭库后展示购买动态',

  /* ── 工具栏 ── */
  'famBuy.toolbar.count': '共 <b>{n}</b> 个游戏 · 第 <b>{page}/{total}</b> 页',
  'famBuy.toolbar.snapshot': '快照数据',
  'famBuy.toolbar.onlyExclusive': '仅独占',
  'famBuy.action.bills': '完整账单分析',
  /* 视图切换（表格 / 封面）：本期只迁文案，控件保持现状不上 HlSegmented */
  'famBuy.view.table': '表格',
  'famBuy.view.cover': '封面',

  /* ── 表格 ── */
  'famBuy.table.name': '游戏名称',
  'famBuy.table.acquired': '购入时间',
  'famBuy.table.buyer': '购买者',
  'famBuy.table.owners': '拥有者',
  'famBuy.table.cnPrice': '国区现价',
  'famBuy.table.ownerCount': '{n}人',

  /* ── 脚注与分页 ── */
  'famBuy.footer.legend': '高亮/NEW = 30 日内入库且独占；购买者 = 最近入库成员',
  'famBuy.pager.prev': '上一页',
  'famBuy.pager.next': '下一页',
} as const

export default famBuy
