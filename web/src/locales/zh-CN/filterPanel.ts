/* ════════════════════════════════════════════════════════════════════
   filterPanel 词条 —— components/business/HlFilterPanel.vue（高级筛选抽屉）。

   覆盖抽屉标题 / 八个分区标题 / 各筛选标签 / 输入框占位符 / 单位词 / 页脚按钮。

   **区服名不在此列**：抽屉里的地区名（`selectedRegion.name`，含「价格范围」弹层
   每一行）来自服务端 GET /api/v1/regions，走 stores/regions 单一来源，属于
   期 7「数据层」范围——本模块不为任何区名建词条。
   ════════════════════════════════════════════════════════════════════ */

const filterPanel = {
  /* 抽屉标题（HlDrawer title）——⚙ 与词同句，整句进词条 */
  'filterPanel.advanced.title': '⚙ 高级筛选',

  /* 分区标题（hl-fp-sec-title） */
  'filterPanel.section.basic': '基础过滤',
  'filterPanel.section.lowest': '史低状态',
  'filterPanel.section.platform': '平台收录',
  'filterPanel.section.priceRange': '价格范围（指定地区）',
  'filterPanel.section.diff': '与国区差价（基于选中地区）',
  'filterPanel.section.reviews': '评测量范围',
  'filterPanel.section.rating': '好评率范围',
  'filterPanel.section.other': '其他',

  /* 基础过滤 —— 勾选项与差价容错行的单位/提示 */
  'filterPanel.basic.top3': '展示前三低价区',
  /* 同一勾选框内的补充说明，随布局模式出现；括号是文案的一部分 */
  'filterPanel.basic.top3ListHint': '(列表视图仅展示最低价)',
  'filterPanel.basic.strictLowest': '绝对低价',
  'filterPanel.basic.tolerance': '差价容错',
  'filterPanel.basic.toleranceUnit': '元',
  'filterPanel.basic.toleranceHint': '（0 表示绝对低价）',

  /* 史低状态（HlCheckbox 变体：新史低蓝勾 / 平史低绿勾） */
  'filterPanel.lowest.new': '新史低',
  'filterPanel.lowest.equal': '平史低',
  'filterPanel.lowest.non': '非史低',

  /* 平台收录 —— 品牌词（Epic/HB/XGP）留在词条内，因为它与中文词连成一句标签 */
  'filterPanel.platform.epic': 'Epic 送过',
  'filterPanel.platform.hb': 'HB 慈善包',
  'filterPanel.platform.xgp': 'XGP 收录',

  /* 与国区差价 */
  'filterPanel.diff.giftOnly': '仅可跨区送礼',
  /* 差价模式下拉的当前值与弹层选项共用同两条词条（同一语义，避免两处漂移） */
  'filterPanel.diff.absolute': '绝对值',
  'filterPanel.diff.percent': '百分比',
  'filterPanel.diff.minPlaceholder': '最低差价',
  'filterPanel.diff.maxPlaceholder': '最高差价',

  /* 评测量范围 —— 「条」跟在数字输入框后独立成元素，故单独成条（英文侧为 reviews） */
  'filterPanel.reviews.unit': '条',

  /* 其他 */
  'filterPanel.other.onlyDiscounted': '仅显示折扣',
  'filterPanel.other.hideOwned': '隐藏已拥有',

  /* 页脚按钮 —— 词条内**保留半角空格**：这是筛选面板的字间距 hack（「重 置」），
     不是漏删的空格，英文侧正常写作 Reset / Apply。 */
  'filterPanel.action.reset': '重 置',
  'filterPanel.action.apply': '应 用',
} as const

export default filterPanel
