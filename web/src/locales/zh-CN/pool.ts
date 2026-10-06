/* pool 词条 —— 监控池页（views/pool/Index.vue）。文案全部取监控语义，「愿望单」只出现在数据来源与类别标记处。
   分节锚点 pool.section.* 一条定义两处消费（data-section 属性值 + 可见小标题）。
   复用：账户徽章沿用 gameCard.status.wishlist/.owned 译法；「绑定」沿用 dashboard 的 bind；
   通用动作走 common.*；「识别到 N 个 AppID」复用 crawl.import.detected/.detectedInvalid。
   不变量：三段同步结果文案各自成条、组件侧不拼片段（「，已自动开始爬取」是随条件增减的整分句）。 */

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

  /* ── 监控池管理（条目增删 + 批量操作）──
     类别词条 `pool.items.kind.*` 一处三用：筛选 chips 的标签、条目标记、
     悬停气泡的来源说明（同一概念三处同词，不另立）。 */
  'pool.items.kind.all': '全部',
  'pool.items.kind.follow': '关注',
  'pool.items.kind.wishlist': '愿望单',
  'pool.items.kind.owned': '已购',
  'pool.items.kind.board': '榜单',
  'pool.items.kind.manual': '手动添加',
  'pool.items.tipAccounts': '追踪账户 {n} 个',
  'pool.items.add': '添加条目',
  'pool.items.addTitle': '添加监控条目',
  'pool.items.addHint':
    '加入监控池的条目全部参与价格爬取；Steam 愿望单里的游戏与点星标关注的游戏优先爬取。支持粘贴或「选择文件」导入（JSON 数组 / 链接 / 裸 AppID 均可），文件导入的 appid 同时计入预设游戏池，随资产种子分发。',
  'pool.items.addPlaceholder': '粘贴 Steam 商店 / SteamDB 链接或裸 AppID（空格、逗号、换行分隔均可）',
  'pool.items.importFile': '选择文件',
  'pool.items.importFileLoaded': '已载入 {name}',
  'pool.items.addSubmit': '加入监控池',
  'pool.items.addNoValid': '没有识别到有效 AppID',
  'pool.items.addResult':
    '已加入监控池：新增 {added} · 恢复 {restored} · 已在池 {exists} · 未识别 {invalid}',
  'pool.items.manage': '管理',
  'pool.items.manageDone': '完成',
  'pool.items.manageHint': '勾选条目后批量移除；移除后不再参与价格爬取。',
  'pool.items.selected': '已选 {n} 项',
  'pool.items.selectPage': '全选本页',
  'pool.items.unselectPage': '取消本页',
  'pool.items.clearSelection': '清空选择',
  'pool.items.removeSelected': '移除选中',
  'pool.items.removeConfirm': '从监控池移除选中的 {n} 款游戏？',
  'pool.items.removeResult': '已移出监控池 {n} 款',

  /* ── 条目格子与空态 ── */
  'pool.items.pendingName': '未抓取（新绑定条目）',
  'pool.items.noMatch': '未找到「{query}」——不在当前关注列表中。',
  'pool.items.noKindMatch': '这个类别下还没有游戏。',
  /* Steam 账户区空态：账号能力专属（绑定后愿望单/已购自动进关注），不冒充全局前置 */
  'pool.accounts.empty': '还没有绑定 Steam 账号——绑定后，愿望单和已购游戏会自动进入关注。',
  'pool.items.empty': '还没有关注任何游戏——到「游戏商店」页点卡片上的星标关注，或点上方「添加条目」。',
} as const

export default pool
