/* Steam 活动日历（views/events/Index.vue + SteamEventCountdown 卡片）。
   活动名本体是数据（后端 nameEn/nameZh），这里只承载界面语义文案。 */

const steamEvents = {
  'steamEvents.updatedAt': '同步于 {time}',
  'steamEvents.refreshing': '数据较旧，正在重新同步',
  'steamEvents.loadFailed': '活动日历暂时获取不到，稍后会自动重试',
  'steamEvents.empty': '暂无已公布的活动',
  'steamEvents.retry': '重试',
  'steamEvents.goCalendar': '查看完整日历 →',
  'steamEvents.noNext': '暂无已公布的未来活动',

  'steamEvents.section.hero': '活动日历',
  'steamEvents.section.live': '进行中',
  'steamEvents.section.upcoming': '即将开始',
  'steamEvents.section.ended': '最近已结束',

  'steamEvents.countdown.label': '距离 {name} 开始',
  'steamEvents.countdown.days': '天',
  'steamEvents.countdown.hours': '时',
  'steamEvents.countdown.minutes': '分',
  'steamEvents.countdown.seconds': '秒',

  'steamEvents.startsToday': '今天开始',
  'steamEvents.startsTomorrow': '明天开始',
  'steamEvents.startsIn': '{n} 天后开始',
  'steamEvents.endsToday': '今天结束',
  'steamEvents.endsIn': '剩 {n} 天',
  'steamEvents.dateRange': '{start} ~ {end}',
  'steamEvents.tzNote': '(北京时间)',

  'steamEvents.category.seasonal_sale': '季节大促',
  'steamEvents.category.next_fest': '新品节',
  'steamEvents.category.themed_fest': '主题特卖',
} as const

export default steamEvents
