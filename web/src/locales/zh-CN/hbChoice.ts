/* hbChoice 词条 —— HB 当月包卡片（components/business/HbChoiceCards.vue）。

   当月标签（如「HB慈善包26年9月包」）是后端下发的事实数据，不进词条；
   卡片标题与两条官方外链整句取词。 */

const hbChoice = {
  'hbChoice.title': 'HB 月包',
  'hbChoice.goMonth': '前往月包',
  'hbChoice.skip': '跳过本月',
  'hbChoice.empty': '当月包尚未入库，等待每日抓取',
} as const

export default hbChoice
