/* achievements 词条 —— 成就殿堂（views/achievements/Index.vue 与 DetailDrawer.vue）。
   术语口径：白金 = 全部成就达成；稀有度按全服解锁占比分档
   （传说 <1% / 极稀有 <5% / 稀有 <20% / 少见 <50% / 常见 ≥50%）。 */
const achievements = {
  /* ── 锚点（data-section）── */
  'achievements.section.overview': '成就总览',
  'achievements.section.analysis': '数据分析',
  'achievements.section.highlights': '成就亮点',
  'achievements.section.games': '游戏成就',

  /* ── KPI 六卡 ── */
  'achievements.kpi.platinum': '白金游戏',
  'achievements.kpi.platinumSub': '全部成就达成',
  'achievements.kpi.games': '有成就游戏',
  'achievements.kpi.gamesSub': '含成就系统的游戏（含库外）',
  'achievements.kpi.total': '成就总数',
  'achievements.kpi.totalSub': '有成就游戏的成就合计',
  'achievements.kpi.unlocked': '已获得成就',
  'achievements.kpi.unlockedSub': '已解锁的成就合计',
  'achievements.kpi.rate': '总完成率',
  'achievements.kpi.rateSub': '已获得 ÷ 成就总数',
  'achievements.kpi.playtime': '总游玩时长',
  'achievements.kpi.playtimeSub': '{n} 款有游玩记录',
  /* 库外（家庭共享等）：合计里含它们，但时长拿不到，故单独点明 */
  'achievements.kpi.externalHint': '其中库外（家庭共享等）{n} 款 · {u} 枚成就 · {p} 个白金',

  /* ── 账号切换（多账号隔离）── */
  'achievements.account.primary': '主账号',
  'achievements.account.family': '家庭成员',

  /* ── 白金殿堂 ── */
  'achievements.shelf.title': '白金殿堂',
  'achievements.shelf.hint': '全部成就达成 · 按达成时间排序',
  'achievements.shelf.date': '白金于 {date}',
  'achievements.shelf.empty': '还没有白金游戏',

  /* ── 图表 ── */
  'achievements.chart.rarityTitle': '成就稀有度分布',
  'achievements.chart.rarityHint': '已获得成就 · 按全服解锁率分档',
  'achievements.chart.playtimeTitle': '游玩时长 Top 10',
  'achievements.chart.playtimeHint': '单位：小时',
  'achievements.chart.unlockTitle': '成就解锁趋势',
  'achievements.chart.unlockHint': '近 24 个月 · 每月解锁枚数',
  'achievements.chart.playtimeTooltip': '{name}：{h} 小时',
  'achievements.chart.unlockTooltip': '{month}：{n} 枚',
  'achievements.chart.rarityTooltip': '{tier}：{n} 枚',

  /* ── 稀有度分档 ── */
  'achievements.rarity.ultra': '传说',
  'achievements.rarity.very_rare': '极稀有',
  'achievements.rarity.rare': '稀有',
  'achievements.rarity.uncommon': '少见',
  'achievements.rarity.common': '常见',
  'achievements.rarity.unknown': '未知',

  /* ── 亮点模块 ── */
  'achievements.rarest.title': '最稀有成就',
  'achievements.rarest.hint': '已获得成就中全服占比最低的',
  'achievements.recent.title': '最近解锁',
  'achievements.recent.hint': '按解锁时间倒序',
  'achievements.recent.empty': '暂无解锁记录',
  'achievements.near.title': '接近白金',
  'achievements.near.hint': '完成度 ≥ {pct}% · 还差几枚即可白金',
  'achievements.near.remaining': '还差 {n} 枚',
  'achievements.highlight.globalPct': '全服 {pct}%',
  'achievements.highlight.rarestBadge': '本作最稀有',

  /* ── 列表工具条 ── */
  'achievements.filter.trophy': '有成就',
  'achievements.filter.platinum': '白金',
  'achievements.filter.progress': '进行中',
  'achievements.filter.external': '库外',
  'achievements.filter.all': '全部',
  'achievements.sort.playtime': '按时长',
  'achievements.sort.progress': '按完成度',
  'achievements.sort.recent': '按最近游玩',
  'achievements.sort.name': '按名称',
  'achievements.search.placeholder': '搜索游戏名',
  'achievements.list.count': '{n} 款游戏',
  'achievements.list.shown': '已显示 {shown} / {total} 款',

  /* ── 同步 ── */
  'achievements.action.sync': '同步成就',
  'achievements.action.more': '显示更多（还有 {n} 款）',
  'achievements.sync.stage.library': '拉取已购库',
  'achievements.sync.stage.progress': '拉取成就进度',
  'achievements.sync.stage.details': '拉取成就明细',
  'achievements.sync.running': '{stage} {done}/{total}',
  'achievements.sync.current': '正在同步：{name}',
  'achievements.sync.failed': '同步失败：{error}',
  'achievements.sync.lastSynced': '上次同步 {date}',

  /* ── 列表行 ── */
  'achievements.list.unlockedOf': '{unlocked}/{total}',
  'achievements.list.neverPlayed': '从未游玩',
  'achievements.list.platinumTag': '白金',
  'achievements.list.externalTag': '库外',
  'achievements.list.externalHours': '时长未知',
  'achievements.list.externalNoRecord': '记录不在库内',
  'achievements.list.lastPlayed': '最近游玩 {date}',

  /* ── 空态 ── */
  'achievements.empty.noCredential': '尚未绑定 Steam 凭证',
  'achievements.empty.noCredentialHint':
    '在「设置」页绑定 Steam Cookie 即可同步；配上 Web API Key 后明细走官方接口，还能查看其他账号的成就',
  'achievements.empty.goSettings': '前往「设置」页',
  'achievements.empty.noData': '还没有成就数据',
  'achievements.empty.noDataHint': '点击「同步成就」拉取你的成就与游玩时长',
  'achievements.empty.noMatch': '没有匹配的游戏',
  'achievements.empty.syncing': '正在同步成就与游玩时长…',

  /* ── 成就明细抽屉 ── */
  'achievements.drawer.unlockedOf': '{unlocked} / {total} 已解锁',
  'achievements.drawer.playtime': '游玩 {h}',
  'achievements.drawer.perfectDate': '白金于 {date}',
  'achievements.drawer.globalPct': '全服 {pct}% 玩家拥有',
  'achievements.drawer.unlockAt': '{date} 解锁',
  'achievements.drawer.locked': '未解锁',
  'achievements.drawer.empty': '该游戏暂无成就数据（尚未同步或无成就系统）',
  'achievements.drawer.groupUnlocked': '已解锁 {n}',
  'achievements.drawer.groupLocked': '未解锁 {n}',
}

export default achievements