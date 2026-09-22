/* dashboard 词条 —— 仪表盘（views/dashboard/Index.vue）。

   分节 key（`section.*`）同时是**页内分节锚点的显示名**：对应区块的
   `data-section` 直接写这个 key（锚点与语言无关，切语言时 ProductTour 的
   选择器不会断），HlSectionRail 读到后用 t() 显示。
   「概览」与「降价速报」两个分节没有可见标题（统计格 / 跑马灯），
   key 只为锚点与悬停气泡存在。

   三处刻意的复用：
   · 四个状态角标（新史低/平史低/折扣/永降）在跑马灯、轮播、动态列表
     三处是同一件事，共用 badge.* 一组；
   · 「最低价区」同时是跑马灯与动态列表里 🏆 图标的 alt；
   · 「全部 →」的箭头留在组件侧，不进词条。

   空库首屏（welcome.*）只在库里一款游戏都没有时渲染，是**参数化整句**：
   中英语序与量词不同，拆成「标签 + 值」多条再拼拼不回去。 */

const dashboard = {
  /* 分节锚点 + 区块标题 */
  'dashboard.section.overview': '概览',
  'dashboard.section.steamFree': 'Steam 免费游戏',
  'dashboard.section.priceDrops': '降价速报',
  'dashboard.section.epicFree': 'Epic 喜加一',
  'dashboard.section.hbChoice': 'HB 月包',
  'dashboard.section.spotlight': '新史低精选',
  'dashboard.section.priceMoves': '降价动态',
  'dashboard.section.rates': '主要汇率',

  /* 统计格（空库时整块不渲染） */
  'dashboard.stats.totalGames': '游戏商店总量',
  'dashboard.stats.discounts': '当前打折',
  'dashboard.stats.monitored': '游戏监控',

  /* 空库首屏：只回答「第一步做什么」 */
  'dashboard.welcome.title': '欢迎来到 {app}',
  'dashboard.welcome.ask': '你想监控哪些游戏？',
  'dashboard.welcome.paste': '粘贴游戏链接添加',
  'dashboard.welcome.sync': '从 Steam 愿望单同步',
  'dashboard.welcome.import': '导入游戏列表 / 文件',
  'dashboard.welcome.pasteHint':
    '一行一个链接（Steam 商店 / SteamDB 链接或 AppID 数字），也可以把整份列表一次粘进来。',
  'dashboard.welcome.add': '添加',
  'dashboard.welcome.adding': '添加中…',
  'dashboard.welcome.added': '已添加 {n} 款，正在获取各区价格',
  'dashboard.welcome.addOwned': '{n} 款已经在库里了',
  'dashboard.welcome.addNone': '没识别出可用的游戏，检查一下链接或 AppID',
  'dashboard.welcome.addFailed': '暂时没能添加，稍后再试一次。',
  'dashboard.welcome.autoTitle': '系统会自动',
  'dashboard.welcome.autoPrice': '获取各区价格',
  'dashboard.welcome.autoRefresh': '定期更新',
  'dashboard.welcome.autoEvent': '发现降价',
  'dashboard.welcome.autoAlert': '到价提醒',

  /* 价格状态角标（跑马灯 / 轮播 / 动态列表共用） */
  'dashboard.badge.newLow': '新史低',
  'dashboard.badge.tieLow': '平史低',
  'dashboard.badge.discount': '折扣',
  'dashboard.badge.permDrop': '永降',

  /* 🏆 冠军区图标的 alt */
  'dashboard.lowestRegion': '最低价区',

  /* 区块右上角入口（箭头 → 留在组件侧） */
  'dashboard.action.viewAll': '全部',

  /* 愿望单同步的结果提示 */
  'dashboard.toast.noAccounts': '还没有绑定 Steam 账号——绑定后可以自动同步愿望单',
  'dashboard.toast.synced': '已同步 {ok}/{total} 个账户，新增 {added} 款',
  'dashboard.toast.crawlTriggerFailed': '新增条目获取价格触发失败（可能已有任务在跑），稍后会自动重试',

  /* 空态 */
  'dashboard.empty.noMoves': '暂无降价动态',
  'dashboard.empty.noRates': '暂无汇率数据（可在汇率页自选追踪币种）',
} as const

export default dashboard