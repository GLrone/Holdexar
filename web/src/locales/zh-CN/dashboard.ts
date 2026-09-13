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

   跑马灯/轮播的机会文案（uptime / proxyHint / toast）都是**参数化整句**：
   中英语序与量词不同，拆成「标签 + 值」多条再拼拼不回去。 */

const dashboard = {
  /* 分节锚点 + 区块标题 */
  'dashboard.section.overview': '概览',
  'dashboard.section.priceDrops': '降价速报',
  'dashboard.section.epicFree': 'Epic 喜加一',
  'dashboard.section.spotlight': '新史低精选',
  'dashboard.section.priceMoves': '降价动态',
  'dashboard.section.rates': '主要汇率',
  'dashboard.section.quickActions': '快捷操作',
  'dashboard.section.sysInfo': '系统信息',

  /* 统计格 */
  'dashboard.stats.totalGames': '游戏商店总量',
  'dashboard.stats.discounts': '当前打折',
  'dashboard.stats.monitored': '游戏监控',
  'dashboard.stats.proxyAvailable': '代理可用',

  /* 运行时长（三种粒度各成一条：秒/分秒/时分秒，中英量词不同） */
  'dashboard.uptime.hms': '{h} 小时 {m} 分 {s} 秒',
  'dashboard.uptime.ms': '{m} 分 {s} 秒',
  'dashboard.uptime.s': '{s} 秒',

  /* 代理统计副注（三段各自成句，用 · 连接——不是同一句被拆开） */
  'dashboard.proxy.clashRunning': 'Clash {n} 出口',
  'dashboard.proxy.clashStopped': 'Clash 未运行',
  'dashboard.proxy.pool': '池 {ok}/{total}',

  /* 价格状态角标（跑马灯 / 轮播 / 动态列表共用） */
  'dashboard.badge.newLow': '新史低',
  'dashboard.badge.tieLow': '平史低',
  'dashboard.badge.discount': '折扣',
  'dashboard.badge.permDrop': '永降',

  /* 🏆 冠军区图标的 alt */
  'dashboard.lowestRegion': '最低价区',

  /* 区块右上角入口（箭头 → 留在组件侧） */
  'dashboard.action.viewAll': '全部',

  /* 快捷操作按钮 */
  'dashboard.action.crawl': '开始爬取',
  'dashboard.action.syncWishlist': '同步监控池',
  'dashboard.action.refreshRates': '刷新汇率',
  'dashboard.action.checkProxies': '代理检测',

  /* 快捷操作的结果提示 */
  'dashboard.toast.crawlStarted': '爬取任务 #{id} 已启动（{count} 款）',
  'dashboard.toast.noAccounts': '未绑定追踪账户，请先在监控池页绑定',
  'dashboard.toast.synced': '已同步 {ok}/{total} 个账户，新增 {added} 款',
  'dashboard.toast.crawlTriggerFailed': '新增条目爬取触发失败（可能已有任务运行），可稍后手动开始',
  'dashboard.toast.ratesRefreshed': '汇率已刷新：{count} 币种（来源 {source}）',
  'dashboard.toast.proxyCheckStarted': '节点体检已触发（{state}）',

  /* 空态 */
  'dashboard.empty.noMoves': '暂无降价动态',
  'dashboard.empty.noRates': '暂无汇率数据（可在汇率页自选追踪币种）',

  /* 系统信息行 */
  'dashboard.sys.app': '应用',
  'dashboard.sys.version': '版本',
  'dashboard.sys.uptime': '运行时长',
} as const

export default dashboard
