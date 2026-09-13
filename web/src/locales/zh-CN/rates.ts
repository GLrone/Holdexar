/* rates 词条 —— 汇率页（views/rates/Index.vue）。

   分节 key（`section.*`）同时是**页内分节锚点的显示名**：对应区块的
   `data-section` 直接写这个 key，HlSectionRail 读到后用 t() 显示。
   其中 `section.rates` / `section.convert` 还兼作区块标题（同文案同一件事）；
   `section.allCurrencies` 只是锚点名——「全部币种（41）」带计数的标题
   是另一条参数化词条（`allCurrencies.title`），两者不能合并。

   时间范围 chips 的 key 单独一组：`RATE_RANGES`（api/client.ts）里的 label
   是中文硬编码，且该文件不在本视图的可改范围内，故视图侧按 `id` 映射到
   本组词条显示，不再读 `r.label`。

   `history.title` / `history.empty` / `chart.yAxis` / `chart.tooltip` 里
   币种名走 `{currency}`（中文名）与 `{code}`（ISO 代号）双占位符：
   中文用前者（「美元 历史走势」），英文用后者（「USD history」）——
   币种中文名来自 api/currencies.ts，那里没有英文名，见报告。 */

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
