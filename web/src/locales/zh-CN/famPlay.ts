/* famPlay 词条 —— 游戏库·游玩页签（views/gamelib/tabs/GlPlay.vue）。
   术语沿用既有译法（英文侧逐字对齐）：时长 → playtime、总游玩时长 → Total playtime、
   人均 X → X per member（不许 average / per-capita 变体）、成员 → member(s)。
   不变量：时长单位 h / kh 不在本模块（非语言中立），落 common.hours / common.hoursK，fmtHours 只决定取哪支；
   famPlay.member.sub 的行内 <b> 写在值里由组件侧 v-html 渲染，不按标记边界拆句。 */
const famPlay = {
  /* ── 空态 ── */
  'famPlay.empty.loading': '家庭库数据拉取中…',
  'famPlay.empty.noPlay': '暂无成员游玩数据——同步家庭组后展示',
  'famPlay.empty.noRecords': '暂无游玩记录',

  /* ── 4 张 KPI 卡 ── */
  'famPlay.kpi.totalLabel': '总游玩时长',
  'famPlay.kpi.totalSub': '全体成员累计',
  'famPlay.kpi.recentLabel': '近2周游玩时长',
  'famPlay.kpi.recentSub': '全体成员合计',
  'famPlay.kpi.avgLabel': '人均近2周',
  'famPlay.kpi.members': '{n} 名成员',
  'famPlay.kpi.topLabel': '近2周最活跃',
  /* 最活跃那格的副注：时长与占比是一句话，拆成两段拼不回去 */
  'famPlay.kpi.topShare': '{h} · 占比 {pct}%',

  /* ── 数据来源与刷新 ── */
  'famPlay.source': '数据来源：Steam Web API（GetOwnedGames playtime）· 客户端游玩后下次同步生效',
  /* ⟳ 图标留在组件侧（纯符号），词条只收它后面的词 */
  'famPlay.action.refresh': '刷新动态',

  /* ── 成员卡 ── */
  'famPlay.memberFallback': '成员{id}',
  'famPlay.member.sub': '库内拥有 <b>{owned}</b> 款 · 近2周游玩 <b>{recent}</b> 款',
  'famPlay.tag.mostActive': '🔥 最活跃',
  'famPlay.tag.noPlay2w': '近2周未游玩',

  /* 进度条行标签 */
  'famPlay.bar.recent': '近2周',
  'famPlay.bar.total': '总计',
} as const

export default famPlay
