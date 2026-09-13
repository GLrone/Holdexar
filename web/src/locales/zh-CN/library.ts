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

  /* 空态 A：库本身为空（诊断监控池 + 爬虫状态） */
  'library.empty.library.title': '游戏商店还是空的',
  'library.empty.library.pool': '监控池有 {n} 款游戏，但还没有爬到价格数据。',
  'library.empty.library.crawling': '爬虫正在运行，抓取完成后会自动出现在这里。',
  'library.empty.library.startHint': '可前往「任务」页启动爬取，或稍等定时任务自动执行。',
  /* 监控池为空时的两句引导（各占一个 <p>，页面里本就是断行的两段） */
  'library.empty.library.noPool1': '监控池没有游戏。先到「任务 → 批量导入」添加 AppID，',
  'library.empty.library.noPool2': '或在「监控池」页绑定 Steam 账户并同步，爬虫会自动抓取入库。',
  'library.empty.library.checking': '正在检查监控池状态…',

  /* 空态 B：有搜索/筛选条件，常规无结果 */
  'library.empty.filter.title': '没有匹配结果',
  'library.empty.filter.hint': '尝试修改搜索条件或价格筛选。',

  /* 无限滚动哨兵 */
  'library.loadingMore': '加载更多...',
  'library.end': '已经到底了',
} as const

export default library
