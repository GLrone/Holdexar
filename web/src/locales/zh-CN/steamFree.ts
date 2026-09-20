/* steamFree 词条 —— Steam 喜加一卡片组（components/business/SteamFreeCards.vue）。

   倒计时是参数化整句：中英「剩 {n} 天 / {n}d left」语序不同，拆「标签 + 值」
   拼不回去，全部整句取词（与 epicFree 同一约定）。

   模块条件渲染：无正在赠送的游戏时整块隐藏（连外壳都不出），因此没有
   空态/失败态词条——出现即信号。 */

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
