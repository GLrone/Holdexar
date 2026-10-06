/* rates 词条 —— 汇率页（views/rates/Index.vue）。
   分节 key（section.*）同时是页内分节锚点的显示名；section.rates / section.convert 兼作区块标题，
   section.allCurrencies 只是锚点名（带计数的标题是另一条参数化词条，不能合并）。
   不变量：RATE_RANGES 的 label 是 api/client.ts 的中文硬编码，视图侧按 id 映射本组词条显示、不读 r.label；
   history / chart 词条里币种名走 {currency}（中文名）与 {code}（ISO 代号）双占位符，中文用前者英文用后者。 */

const rates = {
  /* 分节锚点 + 区块标题 */
  'rates.section.rates': '汇率（对 CNY）',
  'rates.section.history': '历史走势',
  'rates.section.convert': '货币换算',
  'rates.section.allCurrencies': '全部币种',

  /* 页头说明。「上次来源」是可选尾巴，拆成两条整句——
     不要把「（上次来源 {source}）」拼到主句上，中英括号与句末标点位置不同。 */
  'rates.header.desc': '比价换算依据；服务端每日自动抓取全部区服货币，此处自选追踪展示。',
  'rates.header.descWithSource':
    '比价换算依据；服务端每日自动抓取全部区服货币，此处自选追踪展示（上次来源 {source}）。',

  'rates.action.refresh': '立即刷新',

  /* 追踪币种自选 */
  'rates.tracked.label': '追踪币种',
  'rates.tracked.placeholder': '选择要追踪展示的货币',
  'rates.tracked.empty': '未选择追踪币种 —— 在上方「追踪币种」中勾选。',

  /* 历史走势 */
  'rates.history.title': '{currency} 历史走势',
  'rates.history.empty': '暂无 {currency} 历史记录 —— 随每日自动刷新积累。',
  'rates.sourceCaption': '数据来源：{sources}',
  'rates.source.bing': '必应汇率（LSEG 数据）',
  'rates.source.steam': 'Steam 社区数据（AugmentedSteam）',
  'rates.source.erapi': 'Exchangerate-API',
  'rates.source.exh': 'Exchangerate.host',
  'rates.source.seed': '随包历史档案',
  'rates.source.other': '其他来源',

  /* 时间范围 chips（按 RATE_RANGES 的 id 映射，见文件头） */
  'rates.range.oneMonth': '1月',
  'rates.range.sixMonths': '6月',
  'rates.range.oneYear': '1年',
  'rates.range.fiveYears': '5年',
  'rates.range.tenYears': '10年',
  'rates.range.all': '全部',

  /* echarts 轴名与悬停提示窗（tooltip formatter 拼 HTML，同 trendChart 惯例） */
  'rates.chart.yAxis': '{currency} → 人民币',
  'rates.chart.tooltip':
    '{date}<br/>{currency} → 人民币：<b style="color:{color}">{value}</b>',

  /* 货币换算器 */
  'rates.conv.hint': '1 {from} ≈ {rate} {to}',
  'rates.conv.amountPlaceholder': '输入金额',
  'rates.conv.swap': '交换方向',

  /* 全部币种表 */
  'rates.allCurrencies.title': '全部币种（{n}）',
  'rates.table.name': '名称',
  'rates.table.currency': '币种',
  'rates.table.rate': '对 CNY',
  'rates.table.updated': '更新时间',

  /* 刷新结果提示 */
  'rates.toast.refreshed': '已刷新 {count} 个币种（来源 {source}）',
} as const

export default rates
