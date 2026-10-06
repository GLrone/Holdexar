/* famGrowth 词条 —— 家庭组·增长趋势页签（views/family/tabs/FamGrowth.vue）。
   注意：该页签当前在 family/Index.vue 里被注释（不可达），词条照常维护，回归时现成。
   不变量：行内 <b> 写在值里由组件侧 v-html 渲染、按标记边界拆句英文拼不回整句；
   强调色与等宽字留在 CSS（:deep()），.gr-top / .gr-low 按类名回到值里；
   术语沿用既有译法：入库 → acquisition / acquired、时长 → playtime、家庭库 → family library。 */

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
