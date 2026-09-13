/* famInsight 词条 —— 家庭组·成员洞察页签（views/family/tabs/FamInsight.vue）。

   术语沿用 A / B 表（英文侧逐字对齐，见 en/famInsight.ts）：
   · 成员 → member（A 表 / D9，不用 Contributors）
   · 入库 → acquisition / acquired（B 表）
   · 月均 → monthly average、分档/档位 → tier、健康分 → Health score（B 表 / U2）
   · 活跃 / 温热 / 冷淡 / 沉睡 → Active / Warm / Cold / Dormant（B 表 / D7）
   · 家庭库 → family library（A 表 / D1）

   **两组看似重复、实则不同的 key，不要合并**：
   · `status.*`（活跃/温热/冷淡/沉睡）是**成员行右侧的状态徽章**，由
     `stores/familyLib.ts` 的 `memberActivity` 产出——store 只回 key
     （`statusKey`），译文在这里，组件渲染期 `t()` 现取（D7 的冻结陷阱）；
   · `tier.*`（活跃<14天 / …）是**分档统计格**的标签，来自本页自己的
     `STATUS_META` 常量表（那份还带 `<14天` 这类阈值说明），同样只存 key。
     两者别混：徽章要短，统计格要带阈值。

   两处刻意的写法：
   · `famInsight.member.meta` 的行内 `<b>` 写在值里、组件侧 v-html 渲染（同
     bills 的先例）。「共 N 款 · 月均 V」是一条词条，后面的「N 天前最后入库」/
     「从未入库」是另外两条——它们是两个整句、二选一，不是同句被切开的片段；
   · `famInsight.member.last` 的英文写 `{n}d ago` 而不是 `{n} days ago`：
     这个数可能是 0 或 1，英文里 "1 days ago" 是残句，缩写形态对任何 n 都成立，
     也配得上这行 10.5px 的元信息。 */
const famInsight = {
  /* ── 空态 ── */
  'famInsight.empty.loading': '家庭库数据拉取中…',
  'famInsight.empty.noData': '暂无入库时间数据——成员活跃分档依赖 rt_time_acquired',

  /* ── 状态徽章（store 的 memberActivity[].statusKey 取这四条）── */
  'famInsight.status.active': '活跃',
  'famInsight.status.warm': '温热',
  'famInsight.status.cold': '冷淡',
  'famInsight.status.dormant': '沉睡',

  /* ── 分档统计格（本页 STATUS_META 的 labelKey 取这四条，带阈值）── */
  'famInsight.tier.active': '活跃<14天',
  'famInsight.tier.warm': '温热<60天',
  'famInsight.tier.cold': '冷淡<180天',
  'famInsight.tier.dormant': '沉睡/从未',

  /* ── 成员活跃度列表 ── */
  'famInsight.activity.title': '成员入库活跃度',
  'famInsight.memberFallback': '成员{id}',
  'famInsight.member.meta': '共 <b>{total}</b> 款 · 月均 <b>{avg}</b>',
  'famInsight.member.last': '{n} 天前最后入库',
  'famInsight.member.never': '从未入库',

  /* ── 健康分与分档统计 ── */
  'famInsight.health.title': '家庭组健康分',
  'famInsight.health.ring': '健康分',
  'famInsight.tier.title': '活跃分档统计',
  'famInsight.note':
    '口径：距该成员最后一次入库（rt_time_acquired）的天数；健康分 = 四档（100/75/50/25）均值。',
} as const

export default famInsight
