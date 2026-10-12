/* achievements 词条 —— 账号对比 / 解锁动态 / 成就批注（views/achievements/sections/ 三件与 DetailDrawer 批注区）。

   术语口径：批注 = 锚定到具体成就的短评（与生涯便利贴墙互不相干）；
   稀有度档位名复用 achievements.rarity.*，此处不重复定义。 */
const achievementsSocial = {
  /* ── 分节锚点（data-section，侧边定位轨取用）── */
  'achievements.section.social': '猎手社交',

  /* ── 社交页签（Index.vue 三页签容器取用）── */
  'achievements.social.tab.compare': '账号对比',
  'achievements.social.tab.feed': '解锁动态',
  'achievements.social.tab.notes': '猎手批注',

  /* ── 账号对比 ── */
  'achievements.social.compare.title': '账号对比',
  'achievements.social.compare.selectHint': '勾选要对比的账号 · 至多 6 个',
  'achievements.social.compare.empty': '还没有可对比的账号',
  'achievements.social.compare.metric.level': '猎手等级',
  'achievements.social.compare.metric.platinum': '白金',
  'achievements.social.compare.metric.playtime': '游玩时长',
  'achievements.social.compare.metric.challenges': '挑战完成',
  'achievements.social.compare.metric.unlocked': '成就数',
  'achievements.social.compare.metric.rarity': '稀有度分布',

  /* ── 解锁动态 ── */
  'achievements.social.feed.title': '解锁动态',
  'achievements.social.feed.empty': '还没有解锁记录 · 同步成就后这里会亮起来',
  'achievements.social.feed.justNow': '刚刚',
  'achievements.social.feed.minutesAgo': '{n} 分钟前',
  'achievements.social.feed.hoursAgo': '{n} 小时前',
  'achievements.social.feed.daysAgo': '{n} 天前',

  /* ── 成就批注 ── */
  'achievements.note.title': '猎手批注',
  'achievements.note.count': '{n} 条',
  'achievements.note.placeholder': '给这枚成就写点什么…',
  'achievements.note.save': '保存',
  'achievements.note.delete': '删除',
  'achievements.note.confirmDelete': '删除这条批注？',
  'achievements.note.empty': '还没有批注 · 打开成就详情，给值得记一笔的成就留句话',
  'achievements.note.noneMood': '这种心情下还没有批注',
  'achievements.note.maxLen': '{used}/{max}',
  'achievements.note.firstBonus': '首次批注 +{xp} 猎手经验',
  'achievements.note.firstBonusHint': '每枚成就的第一次批注都会得到经验',
  'achievements.note.saveFailed': '保存没有成功，稍后再试一次',
  'achievements.note.mood.love': '心水',
  'achievements.note.mood.wow': '震撼',
  'achievements.note.mood.hard': '硬核',
  'achievements.note.mood.fun': '欢乐',
}

export default achievementsSocial
