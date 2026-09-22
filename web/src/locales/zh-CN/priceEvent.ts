/**
 * 价格事件面（游戏详情「最近价格变化」+ 仪表盘「本轮更新」）。
 *
 * 词条只描述**已经发生的事实**：不加猜测、不把「没抓到」写成「不可用」。
 */
export default {
  /* ── 区块标题（同时用作 data-section 锚点）── */
  'priceEvent.title': '最近价格变化',
  'priceEvent.empty': '最近这一轮没有价格变化',

  /* ── 事件标签：内部枚举不直接出现在界面上 ── */
  'priceEvent.type.PRICE_DROP': '价格下降',
  'priceEvent.type.PRICE_INCREASE': '价格上涨',
  'priceEvent.type.NEW_HISTORICAL_LOW': '历史新低',
  'priceEvent.type.HISTORICAL_LOW_MATCH': '追平史低',
  'priceEvent.type.PERMANENT_PRICE_CHANGE': '原价调整',
  'priceEvent.type.REGION_LOCKED': '进入锁区',
  'priceEvent.type.REGION_UNLOCKED': '解除锁区',
  'priceEvent.type.PRICE_UNAVAILABLE': '暂不售',
  'priceEvent.type.PRICE_RESTORED': '恢复可购买',
  'priceEvent.type.FREE_PROMO': '限时免费',
  'priceEvent.type.REMOVED': '游戏下架',
  'priceEvent.type.OTHER': '价格变化',

  /* ── 状态跃迁的前后值（只出现在后端确实判定出跃迁的事件里）── */
  'priceEvent.state.ok': '可购买',
  'priceEvent.state.locked': '锁区',
  'priceEvent.state.missing': '待补抓',
  'priceEvent.state.blocked': '暂无价格',

  /* ── 相对时间（事件时间来自事件自身的发生时刻）── */
  'priceEvent.ago.justNow': '刚刚',
  'priceEvent.ago.minutes': '{n} 分钟前',
  'priceEvent.ago.hours': '{n} 小时前',
  'priceEvent.ago.days': '{n} 天前',
  'priceEvent.ago.none': '时间未知',

  /* ── 「本轮更新」摘要（仪表盘）── */
  'priceEvent.cycle.title': '本轮更新',
  'priceEvent.cycle.empty': '本轮没有产生价格变化',
  'priceEvent.cycle.running': '本轮更新中',
  'priceEvent.cycle.finished': '{time} 完成',
  'priceEvent.cycle.item': '{label} {n} 次',
  'priceEvent.cycle.eventsTotal': '共 {n} 条变化',
  'priceEvent.cycle.games': '涉及 {n} 款游戏',
  'priceEvent.cycle.truncated': '按最近 {n} 条统计',
  'priceEvent.cycle.more': '查看游戏库',
} as const
