/* epicFree 词条 —— Epic 喜加一卡片组（components/business/EpicFreeCards.vue）。
   不变量：倒计时 / 更新时间为参数化整句；状态章两组四态，章的状态字（live / upcoming）与倒计时句分列
   （英文里一个是词、一个是短语，不硬凑）。 */

const epicFree = {
  'epicFree.title': 'Epic 喜加一',
  'epicFree.updatedAt': '{time} 更新',
  'epicFree.refreshing': '刷新中…',
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
