/* bills 词条 —— 账单页，四个文件共用一个模块（views/bills/Index.vue +
   LedgerTab.vue + CdkTab.vue + TopupTab.vue）。子区块用子命名空间区分：
   bills.ledger.* / bills.cdk.* / bills.topup.*。

   跨文件复用的三条（都在两个以上文件里同义同形，故提到模块顶层）：
   · bills.fxMissing    —— 汇率缺失兜底（LedgerTab 明细行 + TopupTab 流水行）
   · bills.year.all     —— 「全部年份」chip（Index 年份切换 + LedgerTab 年筛选）
   · bills.currencyValue —— 「名称（代号）」币种写法（LedgerTab 明细 + TopupTab 币种卡）

   带 {占位符} 的词条不要在组件侧用 + 或模板字符串拼中文片段：中英语序与量词
   不同（「{count} 笔」对 '{count} transactions'），拼不出版行。金额本身在组件侧
   用 useLocaleFormat() 格式化后作为参数传入，**词条里不放已格式化的数字串**。

   三条词条带行内标记（<b> / <span class="neg"> / <br />），组件侧 v-html 渲染
   （同 bundles.calc.excludeHint 的先例）——整句进词条，不按强调边界切碎：
   · bills.empty.desc           空态说明两段（中间 <br /> 与原文同款）
   · bills.ledger.year.sum / sumRefund   年汇总行（有无退款两个整句二选一）
   · bills.topup.year.sum       充值年汇总行

   图表：`bills.chart.*` 只有标题/描述/空态。echarts option 里两条 series.name
   （当月净支出 / 累计净支出）**刻意未建词条**：本期该 option 属冻结区（期 1 的
   图表主题红线），只迁周边文案。注意 echarts legend 有注册，这两个名字确实会
   显示在图例里——英文界面下仍会显示中文，是一个已知的、本期未处理的缺口。 */

const bills = {
  /* ── 跨文件复用 ── */
  'bills.fxMissing': '汇率缺失',
  'bills.year.all': '全部年份',
  'bills.currencyValue': '{name}（{code}）',

  /* ── 分节锚点（data-section 直接写这串 key，与语言无关）── */
  'bills.section.overview': '账户总览',
  'bills.section.stats': '消费统计',
  'bills.section.chart': '消费走势',
  'bills.section.ledger': '明细台账',

  /* ── 三视图页签（Index.vue 的 TABS）── */
  'bills.tab.ledger': '消费明细',
  'bills.tab.topup': '充值流水',
  'bills.tab.cdk': '许可证',

  /* ── 同步（按钮 / 提示）── */
  'bills.sync.inProgress': '正在同步…',
  'bills.sync.cta': '立即同步账单',
  'bills.sync.ctaShort': '同步账单',
  'bills.sync.success': '已同步「{nickname}」：账单 {bills} 笔 · 入库 {cdk} 个（history {rows} 行）',
  'bills.sync.autoRunning': '自动同步进行中…',
  'bills.sync.lastFailed': '上次同步失败',
  'bills.sync.errorRetry': '{error}（每半小时自动重试）',
  /* 进度气泡各阶段（ Steam 游标翻页不预告总页数，只有实数没有百分比） */
  'bills.sync.stageIdentity': '正在验证账户身份…',
  'bills.sync.stageHistory': '正在拉取账单明细 · 第 {pages} 页 · 已获 {rows} 条',
  'bills.sync.stageLicenses': '正在拉取许可清单…',
  'bills.sync.stageImport': '账单落库中…',

  /* ── 删除账单 ── */
  'bills.delete.success': '已删除「{nickname}」的账单',
  'bills.action.delete': '删除',

  /* ── 空态 ── */
  'bills.empty.title': '还没有任何账单',
  'bills.empty.desc':
    '在<b>设置 → 账户</b>里绑定 Steam Cookie 后，账单会自动拉取入库（消费历史全量翻页 + 许可记录全量分页，无需浏览器扩展）。<br />全部明细按<b>交易当日汇率</b>折算人民币：退款自动冲回、钱包充值单独成流水、礼物/CDK 手动计价。',

  /* ── 账户卷宗头 ── */
  'bills.hero.syncedAt': '同步于 {date}',
  'bills.hero.txCount': '明细 {count} 笔',
  'bills.hero.txCountTip': '服务端解析出的交易笔数',
  'bills.hero.fxMissing': '{count} 笔汇率缺失',
  'bills.hero.valueLabel': '账户价值',
  'bills.hero.valueFormula': '= 自购净额 + CDK·礼物计价',
  'bills.hero.selfNet': '自购净额 {amount}',
  'bills.hero.cdkTotal': 'CDK·礼物 {amount}',

  /* ── 统计卡（label / sub / 悬停 tip）── */
  'bills.stats.net.label': '实际净支出',
  'bills.stats.net.sub': '购买 − 退款',
  'bills.stats.spend.label': '购买总额',
  'bills.stats.spend.sub': '{count} 笔订单',
  'bills.stats.refund.label': '退款合计',
  'bills.stats.refund.sub': '退款率 {rate}',
  'bills.stats.quota.label': '赠礼额度',
  'bills.stats.quota.sub': '自购净额 − 送礼净额',
  'bills.stats.quota.tip':
    '自购净额 {self} − 送出礼物净额 {gift}；为负代表礼金已超出自购留存',
  'bills.stats.topup.label': '充值净额',
  'bills.stats.topup.sub': '{count} 笔流水',

  /* ── 账户切换 chips ── */
  'bills.accounts.fallbackName': '账单 #{id}',
  'bills.accounts.confirmDelete': '删除「{nickname}」的账单及其全部明细？',
  'bills.accounts.deleteTip': '删除该账单',

  /* ── 消费走势区块 ── */
  'bills.chart.title': '消费走势',
  'bills.chart.desc': '柱 = 当月净支出（退款为负冲回），线 = 累计净投入。',
  'bills.chart.empty': '该年份暂无数据',
  /* 图例名。**必须进词条**：本图注册了 LegendComponent（见 Index.vue 的 use([...])），
     这两个名字真的显示在图例里。PriceTrendChart 不译 series.name 的理由是
     「没注册 legend、tooltip 全自定义」，那条理由在 bills 不成立。 */
  'bills.chart.legend.net': '当月净支出',
  'bills.chart.legend.cum': '累计净支出',
  'bills.year.value': '{year} 年',

  /* ── 通用筛选 chip ── */
  'bills.filter.all': '全部',

  /* ═══ 明细台账（LedgerTab.vue）═══ */
  'bills.ledger.warn.more': '等 {count} 条',
  'bills.ledger.type.purchase': '购买',
  'bills.ledger.type.gift': '礼物',
  'bills.ledger.type.ingame': '内购',
  'bills.ledger.type.refund': '退款',
  'bills.ledger.search': '搜索游戏名…',
  'bills.ledger.count': '{count} 笔',
  'bills.ledger.empty': '没有符合条件的交易',
  'bills.ledger.year.sum': '净 <b>¥{net}</b> · {count} 笔',
  'bills.ledger.year.sumRefund':
    '净 <b>¥{net}</b> · 退款 <span class="neg">-¥{refund}</span> · {count} 笔',
  'bills.ledger.month.label': '{month} 月',
  'bills.ledger.month.net': '¥{amount} · {count} 笔',
  'bills.ledger.detail.type': '类型',
  'bills.ledger.detail.currency': '币种',
  'bills.ledger.detail.amount': '金额',
  'bills.ledger.detail.rate': '当日汇率',
  'bills.ledger.detail.rateValue': '1 {code} = {rate} CNY',
  'bills.ledger.detail.discount': '折扣',
  'bills.ledger.detail.origPrice': '折前原价',
  'bills.ledger.detail.payment': '支付方式',
  'bills.ledger.detail.ownership': '归属',
  'bills.ledger.detail.giftRefund': '送礼退款',

  /* ── 命中条（CdkTab + LedgerTab 共用：家庭库关联 + 截至日价格上下文）── */
  'bills.hit.stat': '库存命中',
  'bills.hit.atPrice': '当时 {amount}',
  'bills.hit.lowest': '此前最低 {amount}',
  'bills.hit.atLowest': '当时即史低 {amount}',
  'bills.hit.openDetail': '打开游戏详情',

  /* ═══ 许可证（CdkTab.vue；家庭页「入库许可证 · CDK 花费台账」模块并入本页签）═══ */
  'bills.cdk.stats.priced': '已计价',
  'bills.cdk.stats.total': '计价合计',
  'bills.cdk.hint':
    '账单里没有金额的入库（零售 CDK / 收到的礼物）在此录入实付价，账户价值随之更新；免费入库仅展示、不计价。',
  'bills.cdk.type.cdk': 'CDK {count}',
  'bills.cdk.type.gift': '礼物 {count}',
  'bills.cdk.type.free': '免费 {count}',
  'bills.cdk.type.unpriced': '未填实付',
  'bills.cdk.search': '搜索名称…',
  'bills.cdk.count': '{count} 条',
  'bills.cdk.empty': '没有许可证入库记录（Steam 商店购买在「消费明细」里，不重复建行）',
  'bills.cdk.price.placeholder': '未计价',
  'bills.cdk.price.unit': '¥ 实付',
  'bills.cdk.free.mark': '免费 · 仅展示',
  'bills.cdk.link.store': '商店',
  'bills.cdk.save.success': '已计价「{name}」¥{amount}',

  /* ═══ 充值流水（TopupTab.vue）═══ */
  'bills.topup.stats.total': '充值总额',
  'bills.topup.stats.refund': '充值退款',
  'bills.topup.stats.net': '净充值',
  'bills.topup.stats.currencies': '涉及币种',
  'bills.topup.card.sub': '≈ ¥{amount} · {count} 笔',
  'bills.topup.empty': '该账户没有钱包充值流水',
  'bills.topup.year.sum': '净充值 <b>¥{amount}</b> · {count} 笔',
  'bills.topup.badge.refund': '充值退款',
  'bills.topup.desc.default': '钱包充值',
} as const

export default bills
