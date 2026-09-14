/* trendDrawer 词条 —— 价格走势抽屉（components/business/PriceTrendDrawer.vue）。

   两处刻意的复用，改一条两边同步：
   · 版本标签「标准版」/「Gold 版」在版本下拉与「全部版本」chips 里是同一件事
     （共用 version.standard / version.gold）；带后缀的『标准版（全部）』是另
     一语义（缺省 = 所有 sub 混合序列），单独成条。
   · 「历史最低」同时是图例项与统计格标签，共用 legend.lowest。

   文案里带 {占位符} 的（header.subtitle / price.discount）**不要**在组件侧
   用 + 拼中文片段：中英语序不同，拼不出版行。 */

const trendDrawer = {
  /* 时间范围 chips（值是天数，显示文本走这里） */
  'trendDrawer.range.90d': '90天',
  'trendDrawer.range.1y': '1年',
  'trendDrawer.range.3y': '3年',
  'trendDrawer.range.all': '全部',

  /* 版本标签 */
  'trendDrawer.version.standard': '标准版',
  'trendDrawer.version.gold': 'Gold 版',
  'trendDrawer.version.standardAll': '标准版（全部）',

  /* 史低事件标记 */
  'trendDrawer.event.first': '首发',
  'trendDrawer.event.lowest': '史低',
  'trendDrawer.event.drop': '降至',

  /* 走势卡头部与价格状态 */
  'trendDrawer.header.subtitle': 'AppID {appid} · {region}价格走势',
  'trendDrawer.price.discount': '▼ -{pct}% · 折扣中',
  'trendDrawer.price.noDiscount': '未打折',

  /* 图例与统计格 */
  'trendDrawer.legend.localPrice': '当地价格',
  'trendDrawer.legend.lowest': '历史最低',
  'trendDrawer.stats.current': '当前价',
  'trendDrawer.stats.highest': '区间最高',
  'trendDrawer.stats.lowestHits': '史低次数',
  'trendDrawer.stats.slices': '切片数',

  /* 空态 */
  'trendDrawer.empty.noData': '该版本在此地区暂无历史数据',
  'trendDrawer.empty.hint': '随着定时爬取积累，走势图会逐渐成形。',

  /* 全部版本区块。`versions.title` 同时是**页内分节锚点的显示名**——
     该区块的 `data-section` 直接写这个 key（锚点与语言无关，切语言时
     ProductTour 的选择器不会断），HlSectionRail 读到后用 t() 显示。
     ProductTour 将来要指向本节，target 写 `[data-section="trendDrawer.versions.title"]`。 */
  'trendDrawer.versions.title': '全部版本',
  'trendDrawer.versions.hint': '（当前地区价 · 点击切换走势）',

  /* 抽屉底部详情入口（箭头 → 留在组件侧） */
  'trendDrawer.detail.viewFull': '查看完整详情',
} as const

export default trendDrawer
