/* famWish 词条 —— 家庭组·愿望单页签（views/family/tabs/FamWish.vue）。

   两处刻意的复用，改一条多处同步（同 zh-CN/bundles.ts 的 mps.* 约定）：
   · famWish.tag.*   四个分类标签在**三个位置**是同一件事——筛选片（`HlChip`
     shape="soft"）、KPI 标签（wl-kpi__lbl）、卡片标签（wl-tag）。共用一份，不各写一句。
   · famWish.band.*  价格区间环图的图例。与 FamValue 的 famValue.band.* 是**同一套
     分档**，两个模块的中文写法不同（≥¥200 / ¥200+，FamValue 还多一档「未定价」）——
     **中文保持各自现状**，只要求**英文两侧逐字一致**（见 en/famWish.ts）。两个模块的 key
     不能同名（词典展开后同名即重复 key，是 check-i18n 判据 ①），故各存一份。

   常量表只存 key，不存译文：FILTERS / PRICE_BANDS 是模块级常量，存译文会把语言
   冻在模块加载那一刻（冻结陷阱）。activeFilter 与 filtered 的比较用的也是 **key**
   （'all' / 'owned' / …），不是中文串——只换标签不改比较，切语言就会丢选中态。

   带 {占位符} 的词条不要在组件侧用 + 或模板字符串拼片段：「共 {n} 个」对
   '{n} titles' 拼不出版行。两条分页词条带行内 <b>，组件侧 v-html 渲染
   （同 bills.empty.desc / bundles.calc.excludeHint 的先例）——整句一条词条，
   不按标记边界切碎。强调色留在 CSS：v-html 注入的节点拿不到 scoped 属性，
   故 `.wl-pager` 用 `:deep(b)` 够进去。 */

const famWish = {
  /* 分类标签（筛选片 / KPI 标签 / 卡片标签共用；「全部」复用 common.all） */
  'famWish.tag.familyOwned': '家庭已有',
  'famWish.tag.multiWant': '多人想要',
  'famWish.tag.onSale': '折扣中',
  'famWish.tag.comingSoon': '即将推出',

  /* KPI 行（7 张卡） */
  'famWish.kpi.total': '愿望单总数',
  'famWish.kpi.totalSub': '去重后游戏数',
  'famWish.kpi.hitRate': '{pct}% 命中率',
  'famWish.kpi.multiWantSub': '≥2人共同想要',
  'famWish.kpi.maxDiscount': '最大折扣 -{pct}%',
  'famWish.kpi.noDiscount': '暂无折扣',
  'famWish.kpi.comingSoonSub': '未发售游戏',
  'famWish.kpi.value': '总价值',
  'famWish.kpi.avgPrice': '均价 ¥{amt}',
  'famWish.kpi.members': '参与成员',
  'famWish.kpi.membersFallback': '未同步家庭组（全量追踪）',
  'famWish.kpi.membersSub': '家庭成员数',

  /* 空态 / 加载态 */
  'famWish.empty.loading': '家庭愿望单聚合中…',
  'famWish.empty.noData': '暂无愿望单数据——家庭成员同步愿望单后展示（监控池页可手动同步）',
  'famWish.empty.noMemberData': '暂无成员数据',
  'famWish.empty.noGenreData': '暂无类型数据',
  'famWish.empty.noMatch': '没有匹配的愿望单游戏',

  /* 工具栏 */
  'famWish.toolbar.updatedAt': '更新于 {time}',
  'famWish.toolbar.refresh': '刷新愿望单',
  'famWish.search.placeholder': '搜索游戏名称或 AppID…',

  /* 左栏三张图 */
  'famWish.chart.memberDist': '成员愿望单分布',
  'famWish.chart.topTags': '热门类型 TOP 8',
  'famWish.chart.priceBands': '价格区间分布',
  'famWish.donut.total': '总数',

  /* 价格分档（环图图例；英文须与 famValue.band.* 逐字一致） */
  'famWish.band.free': '免费',
  'famWish.band.lt50': '<¥50',
  'famWish.band.from50to100': '¥50-100',
  'famWish.band.from100to200': '¥100-200',
  'famWish.band.gte200': '≥¥200',

  /* 价格文本与卡片标签 */
  'famWish.price.notListed': '未收录',
  'famWish.tag.wantCount': '{n}人想要',

  /* 分页行（两条二选一：命中超过 60 时提示只显示前 60） */
  'famWish.pager.total': '共 <b>{n}</b> 个',
  'famWish.pager.capped': '共 <b>{n}</b> 个 · 显示前 60',
} as const

export default famWish
