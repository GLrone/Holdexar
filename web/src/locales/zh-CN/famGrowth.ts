/* famGrowth 词条 —— 家庭组·增长趋势页签（views/family/tabs/FamGrowth.vue）。

   ⚠️ 该页签当前在 family/Index.vue 里被注释掉（不可达），词条仍照常维护：
   将来回归时词条现成，且门禁的 CJK 规则对它是 error 级。

   术语对齐表（英文侧逐字对齐，见 en/famGrowth.ts）：
   · 入库（名/形）→ acquisition / acquired；时长 → playtime
   · 家庭库 → family library

   行内 `<b>` 写在值里、组件侧 v-html 渲染（同 bills / rates 的先例）：
   按标记边界把「贡献最多: X (n)」拆成前缀 / 名字 / 括号三段，英文拼不出完整
   句子。强调色与等宽字留在 CSS：v-html 注入的节点拿不到 scoped 属性，
   故 `.gr-compare` / `.gr-span` 用 `:deep()` 够进去；`.gr-top` / `.gr-low`
   按类名回到值里（两个类同时挂在 `.gr-compare` 下，仍是 scoped 规则）。 */

const famGrowth = {
  'famGrowth.member.fallback': '成员{id}',

  'famGrowth.empty.loading': '家庭库数据拉取中…',
  'famGrowth.empty.noData': '暂无入库时间数据——增长趋势依赖 rt_time_acquired',

  'famGrowth.chart.title': '家庭库月度入库累计',
  'famGrowth.legend.total': '总计',

  'famGrowth.compare.top': '贡献最多: <b class="gr-top">{name}</b> ({n})',
  'famGrowth.compare.avg': '平均: <b>{n}</b>',
  'famGrowth.compare.low': '贡献最少: <b class="gr-low">{name}</b> ({n})',

  'famGrowth.span':
    '总计: <b>{n}</b> 条入库 · 首条: <b>{first}</b> → 最新: <b>{last}</b>',
} as const

export default famGrowth
