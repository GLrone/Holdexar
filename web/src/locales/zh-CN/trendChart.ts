/* ════════════════════════════════════════════════════════════════════
   trendChart 词条 —— 价格历史走势图（components/steamhl/PriceTrendChart.vue）。

   tooltip 那一组是**参数化整句**：数值与后端返回的格式化原价由组件传入，
   不拆成「标签 + 值」多条再拼——中英的语序、括号形态都不同，拼不回去。

   注意 series.name 没有词条：它是 echarts 的组件身份标识、用户看不到
   （理由见组件内 SERIES 常量的注释），用户能看到的图例与悬停窗另有其键。

   `legend.lowest`（历史最低）与 trendDrawer 的同名词条是**同一条曲线上的同一件
   东西**，两份英文必须一致；key 分属两个模块是有意的（各自可独立改动），代价是
   这条约束只有人记得——门禁查不出「跨模块同义不同译」。
   ════════════════════════════════════════════════════════════════════ */

const trendChart = {
  'trendChart.empty': '暂无历史数据 —— 随着定时爬取积累，走势图会逐渐成形。',

  /* 详情页大图下方的 HTML 图例 */
  'trendChart.legend.steam': 'Steam 价',
  'trendChart.legend.keyStore': 'Key 店',
  'trendChart.legend.lowest': '历史最低',

  /* 悬停提示窗（echarts tooltip formatter 拼 HTML） */
  'trendChart.tip.lowNode': '史低节点',
  'trendChart.tip.current': '现价 ¥{v}',
  'trendChart.tip.currentWithOriginal': '现价 ¥{v}（{original}）',
  'trendChart.tip.discount': '折扣 -{n}%',
  'trendChart.tip.keyStore': 'Key 店 ¥{v}',
  'trendChart.tip.lowest': '史低 ¥{v}',
} as const

export default trendChart
