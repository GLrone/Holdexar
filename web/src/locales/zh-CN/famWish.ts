/* famWish 词条 —— 家庭组·愿望单页签（views/family/tabs/FamWish.vue）。
   复用：famWish.tag.* 四个分类标签同时用于筛选片 / KPI 标签 / 卡片标签；famWish.band.* 与 famValue.band.*
   同一套分档，中文各保持现状、英文两侧逐字一致（两模块 key 不能同名，同名即 check-i18n 判据①）。
   不变量：FILTERS / PRICE_BANDS 常量只存 key（存译文会把语言冻在模块加载时），比较也用 key 不用译文串；
   带 {占位符} 的词条禁止组件侧拼片段；行内 <b> 整句进词条由组件侧 v-html 渲染，强调色留在 CSS。 */

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
  'famWish.empty.noTagData': '暂无标签数据',
  'famWish.empty.noMatch': '没有匹配的愿望单游戏',

  /* 工具栏 */
  'famWish.toolbar.updatedAt': '更新于 {time}',
  'famWish.toolbar.refresh': '刷新愿望单',
  'famWish.search.placeholder': '搜索游戏名称或 AppID…',

  /* 左栏三张图 */
  'famWish.chart.memberDist': '成员愿望单分布',
  'famWish.chart.topTags': '热门标签 TOP 8',
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
