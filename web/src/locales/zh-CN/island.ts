/* island 词条 —— 灵动岛消息面（components/ui/HlIsland.vue）。
   只收岛内固定动作、任务态文案与空闲态的价格更新结论；消息正文由调用方给出，
   不进词典。

   `island.price.*` 是空闲态的一个产品结论：一个胶囊只表达一件事，不带数量——
   内部计数的口径是刷新轮次与任务批次，不是游戏数，不得以「款」进入用户面；
   job / 队列 / 速度 / worker / 代理也不进这里（技术诊断入口另说）。

   `island.ago.*` 只给相对时长，不给具体时刻：岛是常驻面，绝对时刻会随窗口
   停留而失真，相对时长每 60s 重算一次。 */
const island = {
  'island.label': '消息提示',
  'island.retry': '重试',
  'island.view': '查看',
  'island.viewLogs': '查看日志',
  'island.dismiss': '忽略',
  'island.mute': '静音此类',
  'island.more': '还有 {n} 条',
  'island.task.running': '正在抓取价格 {done}/{total}',
  'island.task.cancel': '取消任务',
  'island.task.open': '任务中心',

  /* 空闲态：价格更新结论 */
  'island.price.waiting': '等待更新',
  'island.price.running': '正在更新游戏价格',
  'island.price.done': '价格已更新',
  'island.price.partial': '更新未完成',
  'island.price.tip': '部分地区价格暂未更新，系统会稍后自动重试',
  'island.price.tipDone': '本次更新已完成',

  /* 展开态正文：数据有多旧 */
  'island.price.updatedAt': '上次更新 {time}',
  'island.ago.justNow': '刚刚',
  'island.ago.minutes': '{n} 分钟前',
  'island.ago.hours': '{n} 小时前',
  'island.ago.days': '{n} 天前',
} as const

export default island
