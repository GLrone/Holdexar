/* famValue 词条 —— 家庭组·价值页签（views/family/tabs/FamValue.vue）。

   · famValue.band.* 与 famWish.band.* 是**同一套价格分档**。中文两侧写法本来就不同
     （此处 ¥200+ / ≥¥200）且此处多一档「未定价」——**中文保持原样**，只要求**英文
     两侧逐字一致**（两侧同写 '¥200+'）。key 不能同名（展开后是重复 key），故各存一份。
   · BAND_KEYS 只存 key：模块级常量存译文会把语言冻在模块加载那一刻（冻结陷阱）。
   · famValue.kpi.paidSave 值里带行内 <span>，其颜色取组件传入的 {color}
     （**本次唯一保留行内样式的词条**）：那抹绿是色板 PALETTE.green，
     **不能在词典里写裸十六进制**（familyColors.ts 的约定：各处取用一律走名字），
     故由组件侧当参数传入；同时金额也是组件侧格式化后传入的
     （同 bills.ledger.year.sum 的做法）。组件侧 v-html 渲染。
   · famValue.rank.note 同理带行内 <b>，整句一条词条，组件侧 v-html 渲染；它的
     强调色留在 CSS（v-html 注入的节点拿不到 scoped 属性，故用 `:deep(b)` 够进去）。 */

const famValue = {
  /* 空态 / 加载态 */
  'famValue.empty.loading': '家庭库数据拉取中…',
  'famValue.empty.noData': '暂无数据——同步家庭组后展示价值洞察',
  'famValue.empty.noPlaytime': '暂无游玩数据',
  'famValue.empty.noMemberData': '暂无成员数据',

  /* KPI 四卡。
     口径：store.games = 家庭库（共享库 ∪ 成员已购的并集，见后端 family/service.py
     的 set(shared_map) | set(owned_by_app)），故第一张卡的标签是「家庭库总价值」——
     原「共享库总价值」与口径不符，是中文侧的既有 bug（期 6 裁决 D1）。
     第四张卡的副标题是公式语境，「总时长」保留不改（裁决 D2）。 */
  'famValue.kpi.libraryValue': '家庭库总价值（原价）',
  'famValue.kpi.paidSave': '实付 ¥{paid} · 省 <span style="color: {color}">{pct}%</span>',
  'famValue.kpi.perMember': '人均贡献价值',
  'famValue.kpi.perMemberSub': '按原价计算 · {n} 名成员均摊',
  'famValue.kpi.recent90': '近90天新增价值',
  'famValue.kpi.recent90Sub': '最近 90 天入库原价合计',
  'famValue.kpi.perHour': '平均性价比',
  'famValue.kpi.perHourSub': '实付 ÷ 总时长 {h}h',

  /* 入库价值趋势柱（月份轴标签走 useLocaleFormat().month()，不建词条） */
  'famValue.trend.title': '月份活跃价值分布',
  'famValue.trend.tooltip': '{month} · {n} 款活跃 · ¥{amt}',
  'famValue.trend.note': '按入库月份（rt_time_acquired）聚合的原价合计——入库节奏的价值维度。',

  /* 价格 × 游玩时长散点 */
  'famValue.scatter.title': '价格 × 游玩时长散点',
  'famValue.scatter.yAxis': '时长',
  'famValue.scatter.xAxis': '价格(¥) →',
  'famValue.scatter.note': '圆点大小 = 游玩时长；左上区域 = 高性价比（低价长玩），右下 = 低性价比（高价少玩）。',
  'famValue.scatter.dot': '{name} {price} · {h}h',

  /* 价格区间 × 月份 热力 */
  'famValue.heat.title': '价格区间 × 月份 活跃热力',
  'famValue.heat.rowLabel': '价格档',
  'famValue.heat.low': '少',
  'famValue.heat.high': '多',

  /* 成员价值贡献排名 */
  'famValue.rank.title': '成员价值贡献排名',
  'famValue.rank.memberFallback': '成员{id}',
  'famValue.rank.note': '<b>口径说明：</b>价值 = 成员拥有游戏的国区现价合计（多人共享按拥有者分别计入）。',

  /* 价格分档（热力图行标签；英文须与 famWish.band.* 逐字一致） */
  'famValue.band.free': '免费',
  'famValue.band.lt50': '<¥50',
  'famValue.band.from50to100': '¥50-100',
  'famValue.band.from100to200': '¥100-200',
  'famValue.band.gte200': '¥200+',
  'famValue.band.unpriced': '未定价',
} as const

export default famValue
