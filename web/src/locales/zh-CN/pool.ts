/* pool 词条 —— 监控池页（views/pool/Index.vue）。

   本页三个分节（Steam 账户 / 监控地区 / 监控条目）共同构成「监控池」的
   完整链路：绑定数据源 → 圈定抓取区服 → 浏览池内条目。页面名义是监控池
   ——愿望单只是数据源之一（账户 kinds 同时有 wishlist/owned 两类），
   文案全部取监控语义，「愿望单」一词只在描述数据来源处出现。

   三个分节锚点 `pool.section.*` 是**共用**词条：既是 data-section 的属性值
   （HlSectionRail 当 key 解释后渲染成气泡/aria-label），又是可见的
   小标题本身，本页这两处文案逐字相同，故不另立重复条目。

   几处刻意的对齐（跨模块同义分歧靠人维护，门禁看不出来）：
   · 账户的「愿望单 / 已购」徽章沿用 gameCard.status.wishlist / .owned
     的既有译法（kinds 的两个维度都是 Steam 侧的原文概念）；
   · 「绑定」这一动作取 dashboard 页的英文说法 bind（dashboard.toast.noAccounts
     的引导正指向本页）；
   · 监控地区的三件动作（保存 / 全选 / 清空）是通用动作，走 common.*，
     不在本模块重复立条。

   三段同步结果文案（新增/新增+爬取/无新增）各自成条，不在组件侧用 + 拼：
   「，已自动开始爬取」是随条件增减的一整个分句，中英语序不同。 */

const pool = {
  /* ── 分节锚点（data-section 属性值 + 可见小标题共用）── */
  'pool.section.steamAccount': 'Steam 账户',
  'pool.section.regions': '监控地区',
  'pool.section.items': '监控条目',

  /* ── Steam 账户分节 ── */
  'pool.account.desc':
    '绑定后自动同步该账户的愿望单与已购游戏库进监控池（需对方资料公开），作为爬取的数据源。',
  'pool.account.steamidPlaceholder': 'Steam 好友码 / SteamID64 / 个人资料 URL',
  'pool.account.labelPlaceholder': '备注名（可选）',
  'pool.account.bind': '绑定',
  'pool.account.bindSuccess': '已绑定 {code}',
  'pool.account.unbindTitle': '确认',
  'pool.account.unbindConfirm': '解除绑定 {name}？其监控条目将一并删除。',
  'pool.account.unbindSuccess': '已解除绑定',
  'pool.account.sync': '同步',
  'pool.account.syncAdded': '同步完成：新增 {n} 款',
  'pool.account.syncAddedCrawling': '同步完成：新增 {n} 款，已自动开始爬取',
  'pool.account.syncNoNew': '同步完成：监控池 {n} 款，无新增',
  'pool.account.neverSynced': '未同步',
  'pool.account.friendCode': '好友码 {code}',
  'pool.account.kindWishlist': '愿望单',
  'pool.account.kindOwned': '已购',
  'pool.account.colAccount': '账户',
  'pool.account.colItemCount': '监控条目',
  'pool.account.colLastSync': '上次同步',
  'pool.account.colActions': '操作',

  /* ── 监控地区分节 ── */
  'pool.regions.desc':
    '价格抓取严格按勾选的区服执行，未勾选的区一律不爬；全部清空后爬取任务会拒绝启动。',
  'pool.regions.selected': '已选 {n} / {total} 区',
  'pool.regions.savedNone': '已保存：未启用任何区服，爬取任务将拒绝启动',
  'pool.regions.savedStrict': '已保存：爬取将严格按启用的区服执行',
  'pool.regions.searchPlaceholder': '搜索地区名或代码',
  'pool.regions.noMatch': '没有匹配的区服',

  /* ── 监控条目分节的标题区 ── */
  'pool.items.matchCount': '匹配 {matched} / 共 {total} 款',
  'pool.items.totalHint': '共 {total} 款 —— 点击查看价格详情。',
  'pool.items.searchPlaceholder': '搜索游戏名或 AppID',
  'pool.items.crawlAll': '全量爬取监控池',

  /* ── 条目格子与空态 ── */
  'pool.items.pendingName': '未抓取（新绑定条目）',
  'pool.items.noMatch': '未找到「{query}」—— 不在当前监控条目中。',
  'pool.items.empty': '暂无监控条目 —— 绑定账户并同步即可开始。',
} as const

export default pool
