/* library 词条 —— views/library/Index.vue（游戏库：Navbar + 筛选 + 卡片网格 + 无限滚动）。

   本页文案只有两类：统计条 / 状态与空态引导。空态分「库本身为空」（含监控池与
   爬虫的诊断引导）与「筛选无结果」两支，英文侧据此重写而非直译。

   复用：「重试」走 common.retry。 */

const library = {
  /* Navbar 统计条（总数 + 已加载），数字由 useLocaleFormat 预格式化后传入 */
  'library.stats': '共 {total} 款 · 已加载 {loaded}',

  /* 加载失败（错误消息来自后端，作为参数传入） */
  'library.error.title': '加载失败',
  'library.error.network': '无法连接到服务器',

  /* 空态 A：库本身为空（诊断监控池与更新状态，给引导） */
  'library.empty.library.title': '游戏商店还是空的',
  /* 网络提示：只在「已加游戏却长时间没数据」时出现。代理是改善手段不是前提
     （直连为标准形态），措辞留有余地（「可能」「通常」），不写死因果 */
  'library.empty.library.netHint':
    '长时间没有数据？可能是当前网络访问 Steam 受限——配置代理通常可以改善。',
  'library.empty.library.goProxy': '去配置代理',
  /* 监控池为空：手动导入的直达按钮（与 noPool1/2 同语义，点过去就对了） */
  'library.empty.library.goImport': '去添加游戏',
  'library.empty.library.pool': '监控池有 {n} 款游戏，但还没有价格数据。',
  'library.empty.library.crawling': '正在更新价格，完成后会自动出现在这里。',
  'library.empty.library.startHint': '系统会按固定节奏自动更新价格；也可以到「任务」页立即更新一次。',
  /* 监控池为空时的两句引导（各占一个 <p>，页面里本就是断行的两段） */
  'library.empty.library.noPool1': '先把想比价的游戏加进来：',
  'library.empty.library.noPool2': '到「任务」页粘贴游戏链接即可；绑定过 Steam 账号的话，愿望单和已购游戏也会自动进来。',
  'library.empty.library.checking': '正在检查监控池状态…',

  /* 空态 B：有搜索/筛选条件，常规无结果 */
  'library.empty.filter.title': '没有匹配结果',
  'library.empty.filter.hint': '尝试修改搜索条件或价格筛选。',

  /* 无限滚动哨兵 */
  'library.loadingMore': '加载更多...',
  'library.end': '已经到底了',
} as const

export default library
