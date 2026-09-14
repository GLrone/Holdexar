/* epicFree 词条 —— Epic 喜加一卡片组（components/business/EpicFreeCards.vue）。

   倒计时/更新时间是参数化整句：中英「剩 {n} 天 / {n}d left」语序不同，
   拆「标签 + 值」拼不回去，全部整句取词。

   状态章两组四态（进行中/即将开始 × 剩 N 天/N 天后开始），章的状态字
   （live/upcoming）与倒计时句分列：英文里一个是词、一个是短语，不硬凑。 */

const epicFree = {
  'epicFree.title': 'Epic 喜加一',
  'epicFree.updatedAt': '{time} 更新',
  'epicFree.free': '免费',
  'epicFree.live': '进行中',
  'epicFree.upcoming': '预告',
  'epicFree.endsIn': '剩 {n} 天',
  'epicFree.endsToday': '今天结束',
  'epicFree.startsIn': '{n} 天后开始',
  'epicFree.startsTomorrow': '明天开始',
  'epicFree.startsToday': '今天开始',
  'epicFree.startsOn': '{date} 开始',
  'epicFree.claim': '前往领取',
  'epicFree.goHub': '免费游戏总览',
  'epicFree.empty': '当前没有白送游戏',
  'epicFree.loadFailed': '喜加一获取失败，每小时自动重试',
  'epicFree.mobile': '移动端',
  'epicFree.mobileCard': '本周移动白送',
} as const

export default epicFree
