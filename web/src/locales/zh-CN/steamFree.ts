/* steamFree 词条 —— Steam 喜加一卡片组（components/business/SteamFreeCards.vue）。
   不变量：倒计时为参数化整句（与 epicFree 同一约定）；无正在赠送的游戏时整块隐藏，故没有空态 / 失败态词条——出现即信号。 */

const steamFree = {
  'steamFree.title': 'Steam 喜加一',
  'steamFree.updatedAt': '{time} 更新',
  'steamFree.free': '免费',
  'steamFree.live': '赠送中',
  'steamFree.endsInDays': '剩 {n} 天',
  'steamFree.endsInHours': '剩 {n} 小时',
  'steamFree.endsToday': '今天结束',
} as const

export default steamFree
