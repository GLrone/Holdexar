/* 自动抓取页词条 —— views/fetch/Index.vue。

   页面回答一个问题：「Holdexar 会自动联网帮我更新哪些内容」。
   文案面向新手：不出现 抓取/调度/任务/快照/档案 等实现词，
   每条描述说清「这是什么 + 开着对你有什么用 + 关了会怎样」。 */

const fetchPage = {
  'fetch.desc':
    '下面这些内容，Holdexar 会自动联网帮你更新。用不上的就关掉：已经拿到的数据不会丢，手动刷新照常能用。',
  'fetch.epic.label': 'Epic 免费游戏',
  'fetch.epic.desc':
    'Epic 商店经常送免费游戏。开着它，首页会自动告诉你现在能免费领哪些。',
  'fetch.hb.label': 'Humble Choice 每月合集',
  'fetch.hb.desc':
    'Humble 每月推出一批游戏合集。开着它，每月自动整理当月有哪些游戏，并发一封清单邮件。',
  'fetch.boards.label': 'Steam 排行榜',
  'fetch.boards.desc':
    '自动获取 Steam 的热销、特惠、新品和即将推出排行榜，「游戏商店」里最近热门的游戏就来自这里。',
  'fetch.bundleCounts.label': '捆绑包记录',
  'fetch.bundleCounts.desc':
    '记录每款游戏参加过哪些优惠合集，显示在捆绑包页和游戏详情里。关掉后这些记录不再更新。',
  'fetch.fx.label': '汇率更新',
  'fetch.fx.desc':
    '每天凌晨自动获取最新汇率，用来把外币价格换算成人民币。关掉后按最近一次的汇率换算，汇率页也可以随时手动刷新。',
  'fetch.fxHistory.label': '汇率历史补全',
  'fetch.fxHistory.desc':
    '历史汇率有缺漏时自动联网补齐，让历史价格换算更准确。默认使用免密钥的公共数据源，无需任何配置。',
  'fetch.interval.label': '价格更新间隔',
  'fetch.interval.desc':
    '多久自动刷新一次各游戏的价格。间隔越短，价格越新鲜，联网也越频繁。「任务」页的自动价格更新关闭时，这项不生效。',
  'fetch.interval.hours': '每 {n} 小时',
  'fetch.interval.toast': '已更新：价格每 {n} 小时自动刷新一次',
  'fetch.catalog.label': '未关注游戏一并更新',
  'fetch.catalog.desc':
    '默认开启：每次价格更新先刷关注的游戏，再一并获取游戏库里没在关注、也没下架游戏的现价（价格更新间隔对它们同样生效），Steam 特惠榜去重后排在每轮最后，只补榜上多出来的新游戏。关闭后只更新关注的游戏，特惠榜差值一并停。改动从下一轮价格更新起生效。',
  'fetch.catalog.toastOn': '已开启：下一次价格更新起，未关注游戏与特惠榜差值一并刷新',
  'fetch.catalog.toastOff': '已关闭：下一次价格更新起只更新关注的游戏',
  'fetch.reset': '恢复默认',
  'fetch.reset.toast': '已恢复默认：全部内容源开启，价格每 6 小时刷新',
  'fetch.toast.on': '已开启自动更新',
  'fetch.toast.off': '已关掉：不再自动更新，已有数据保留',
  'fetch.toast.failed': '没保存上，请再试一次',
} as const

export default fetchPage
