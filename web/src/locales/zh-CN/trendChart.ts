/* trendChart 词条 —— 价格历史走势图（components/business/PriceTrendChart.vue）。
   不变量：tooltip 一组是参数化整句（数值与格式化原价由组件传入），不拆「标签 + 值」拼接；
   series.name 无词条（echarts 组件身份标识，用户不可见；用户可见的图例与悬停窗另有其键）；
   legend.lowest 与 trendDrawer 同名词条是同一条曲线上的同一件事，英文必须逐字一致。 */

const trendChart = {
  'trendChart.empty': '暂无历史数据 —— 随着定时爬取积累，走势图会逐渐成形。',

  /* 详情页大图下方的 HTML 图例 */
  'trendChart.legend.steam': 'Steam 价',
  'trendChart.legend.keyStore': 'Key 店',
  'trendChart.legend.lowest': '历史最低',

  /* 缩略导航条（全量走势缩略 × 时间窗控制二合一）。
     range 是参数化整句：起止月份 + 跨度，跨度整段时是「全部」、否则是月数 */
  'trendChart.thumb.title': '全局历史走势缩略',
  'trendChart.thumb.hint': '拖拽滑块选择显示区间',
  'trendChart.thumb.all': '全部',
  'trendChart.thumb.months': '{n} 个月',
  'trendChart.thumb.range': '{start} → {end}（{span}）',

  /* 悬停提示窗（echarts tooltip formatter 拼 HTML） */
  'trendChart.tip.lowNode': '史低节点',
  'trendChart.tip.current': '现价 ¥{v}',
  'trendChart.tip.currentWithOriginal': '现价 ¥{v}（{original}）',
  'trendChart.tip.discount': '折扣 -{n}%',
  'trendChart.tip.keyStore': 'Key 店 ¥{v}',
  'trendChart.tip.lowest': '史低 ¥{v}',
} as const

export default trendChart
