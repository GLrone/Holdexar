/* English 词典 · famValue（与 zh-CN/famValue.ts 同构，key 必须逐一对齐）。
   对应源文件：views/family/tabs/FamValue.vue。

   ⚠️ famValue.band.* 与 famWish.band.* 是同一套价格分档，**英文必须逐字一致**
   （本例中文两侧不统一：此处 ¥200+、FamWish 写 ≥¥200）。故 band.gte200 两侧同写
   '¥200+'，不从各自的中文写法直译。

   术语取自期 6 的 A / B 表，不另立译法：
   · 家庭库 = family library（D1；本页统计的就是家庭库＝共享库 ∪ 成员已购的并集）
   · 原价 = base price / 现价 = current price（A 表 / B 表）
   · 入库 = acquired（B 表，同 acquisition heatmap）
   · 时长 = playtime / 总时长 = Total playtime（B 表 + D2）
   · 人均 X = X per member（D3）／档位 = tier（B 表）／热力图 = heatmap（B 表）
   · 排行 = ranking / 价值 = value（B 表）／价值洞察 = value insights（A 表）
   · 成员 = members（D9；不用 Contributors / per capita）

   kpi.paidSave 与 rank.note 的值带行内标记（<span> 与 <b>），组件侧 v-html
   渲染——整句一条词条，不按强调边界切碎。
   paidSave 的 {color} 必须留在值里（色板由组件当参数传入，词典不写裸十六进制）；
   rank.note 的 <b> 只带标记，强调色在 CSS 里（`.vi-rank-note :deep(b)`）。 */

import type { MessageKey } from '../zh-CN'

const famValue: Partial<Record<MessageKey, string>> = {
  /* Empty / loading states */
  'famValue.empty.loading': 'Loading family library…',
  'famValue.empty.noData': 'No data yet — sync your Family to see value insights',
  'famValue.empty.noPlaytime': 'No playtime data',
  'famValue.empty.noMemberData': 'No member data',

  /* Four KPI cards */
  'famValue.kpi.libraryValue': 'Family library value (base price)',
  'famValue.kpi.paidSave': 'Paid ¥{paid} · saved <span style="color: {color}">{pct}%</span>',
  'famValue.kpi.perMember': 'Value per member',
  'famValue.kpi.perMemberSub': 'Based on base price · split across {n} members',
  'famValue.kpi.recent90': 'Added in the last 90 days',
  'famValue.kpi.recent90Sub': 'Base price of titles acquired in the last 90 days',
  'famValue.kpi.perHour': 'Cost per hour',
  'famValue.kpi.perHourSub': 'Paid ÷ Total playtime ({h} hrs)',

  /* Value-by-acquisition-month bars */
  'famValue.trend.title': 'Value by acquisition month',
  'famValue.trend.tooltip': '{month} · {n} acquired · ¥{amt}',
  'famValue.trend.note':
    'Base price totals aggregated by acquisition month (rt_time_acquired) — the value side of acquisition pace.',

  /* Price × playtime scatter */
  'famValue.scatter.title': 'Price × playtime scatter',
  'famValue.scatter.yAxis': 'Playtime',
  'famValue.scatter.xAxis': 'Price (¥) →',
  'famValue.scatter.note':
    'Dot size = playtime. Top-left = great value (cheap and long); bottom-right = poor value (expensive and barely played).',
  'famValue.scatter.dot': '{name} {price} · {h} hrs',

  /* Price × month heatmap */
  'famValue.heat.title': 'Price tier × month heatmap',
  'famValue.heat.rowLabel': 'Price tier',
  'famValue.heat.low': 'Low',
  'famValue.heat.high': 'High',

  /* Member value ranking */
  'famValue.rank.title': 'Member value ranking',
  'famValue.rank.memberFallback': 'Member {id}',
  'famValue.rank.note':
    '<b>Basis:</b> Value = the current CN price of the games a member owns; a shared title counts once for each owner.',

  /* Price bands (heatmap row labels; must match famWish.band.* word for word) */
  'famValue.band.free': 'Free',
  'famValue.band.lt50': '<¥50',
  'famValue.band.from50to100': '¥50-100',
  'famValue.band.from100to200': '¥100-200',
  'famValue.band.gte200': '¥200+',
  'famValue.band.unpriced': 'Unpriced',
}

export default famValue
