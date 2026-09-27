/* library 词条 —— views/library/Index.vue（游戏库：Navbar + 筛选 + 卡片网格 + 无限滚动）。

   本页文案只有两类：统计条 / 状态与空态引导。空态分「库本身为空」（含监控池与
   爬虫的诊断引导）与「筛选无结果」两支，英文侧据此重写而非直译。

   复用：「重试」走 common.retry。 */

const library = {
  /* Navbar 统计条（总数 + 已加载），数字由 useLocaleFormat 预格式化后传入 */
  'library.stats': '共 {total} 款 · 已加载 {loaded}',

  /* 加载失败（错误消息来自后端，作为参数传入） */
  'library.error.title': '暂时无法加载游戏',
  'library.error.network': '无法连接到服务器',

  /* 空态 A：库本身为空（诊断监控池与更新状态，给引导） */
  /* 空态 A：库里没有游戏（P-M5）——只给用户动作与结果，不提监控池/任务/代理，
     添加动作与仪表盘欢迎卡同链（粘贴直添，导入列表走关注页批量入口） */
  /* 空态 B：刚添加还没拿到价格（新导入行要等首轮取价补全才可见）——
     不是「没有游戏」，必须让用户看到系统接住了 */
  'library.empty.library.pending': '正在获取价格',
  'library.empty.library.pendingHint': '添加成功，游戏会自动出现在这里。',
  'library.empty.library.title': '还没有游戏',
  'library.empty.library.hint': '添加游戏后，系统会自动获取各地区价格。',
  'library.empty.library.paste': '粘贴游戏链接',
  'library.empty.library.import': '导入游戏列表',

  /* 空态 C：有搜索/筛选条件但没匹配（与「库里没有游戏」分开说，给清除入口） */
  'library.empty.filter.title': '没有找到符合条件的游戏',
  'library.empty.filter.hint': '试试调整搜索词，或清除筛选条件。',
  'library.empty.filter.clear': '清除搜索与筛选',

  /* 无限滚动哨兵 */
  'library.loadingMore': '加载更多...',
  'library.end': '已经到底了',

  /* ── 目录移除（假删除）：浏览中逐卡移除 + 批量整理 + 已移除视图恢复 ──
     用户词汇只用「移除 / 恢复」；账本、排除等实现细节不进文案 */
  'library.manage': '批量整理',
  'library.manage.done': '完成整理',
  'library.manage.selected': '已选 {n} 款',
  'library.manage.selectAll': '全选已加载',
  'library.manage.unselectAll': '取消全选',
  'library.manage.clearSelection': '清除选择',
  'library.manage.removeSelected': '移除所选',
  'library.manage.removeConfirm': '移除所选 {n} 款游戏？之后可在「已移除」里恢复。',
  'library.manage.hint': '点卡片勾选，一次移除不再想要的款。',
  'library.removed.view': '已移除',
  'library.removed.empty': '没有移除过游戏',
  'library.removed.emptyHint': '浏览时点卡片上的移除按钮，移除的游戏会收进这里，随时可恢复。',
  'library.removed.toast': '已移除 {n} 款',
  'library.removed.restoreResult': '已恢复 {n} 款',
  'library.removed.fail': '移除失败，请重试',
  'library.removed.restoreFail': '恢复失败，请重试',
} as const

export default library
