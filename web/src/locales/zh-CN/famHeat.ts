/* famHeat 词条 —— 家庭组·入库热力图页签（views/family/tabs/FamHeat.vue）。

   术语沿用 A / B 表（英文侧逐字对齐，见 en/famHeat.ts）：
   · 入库 → acquisition / acquired（acquisition heatmap 同源）
   · 快照数据 → Snapshot（代码 `fromSnapshot`）
   · 家庭库 → family library

   三处刻意的写法：
   · 月份轴标签（原手写的 `2024年1月` / `1月`）不是词条，走
     `useLocaleFormat().yearMonth()` / `.month()`——中文 `2024年1月` / `1月`、
     英文 `Jan 2024` / `Jan`（U3）。`YYYY-MM-DD` 那串日期格式（`fmtKey`）保持
     原样：它在两种语言下是同一串 ISO 文本，语言中立，**有意不做词条**；
   · `famHeat.meta` 的行内 `<b>` 写在值里、组件侧 v-html 渲染（同 bills 的先例）。
     日期区间与 `→` 留在组件侧：`→` 是纯符号，且那两个日期由
     `fmtKey` 就地截取，不做词条；
   · `famHeat.tip.day` 是**整句**（`{date} · 入库 {count} 款`），不是「日期 + 文案」
     两段拼接——英文语序不同。 */
const famHeat = {
  /* ── 空态 ── */
  'famHeat.empty.loading': '家庭库数据拉取中…',
  'famHeat.empty.noData': '暂无入库时间数据（同步家庭库后，Steam 返回 rt_time_acquired 时展示）',
  'famHeat.empty.member': '该成员暂无入库时间数据',

  /* ── 成员筛选与标题 ── */
  'famHeat.memberFallback': '成员{id}',
  'famHeat.filter.all': '全部成员',
  'famHeat.title.family': '家庭库入库动态',
  'famHeat.tag.snapshot': '快照数据',

  /* ── 图块元信息（行内 <b> 在值里，v-html 渲染；日期区间与 → 留在组件侧）── */
  'famHeat.meta': '共 <b>{total}</b> 条入库 · 峰值 {peak}/天',
  /* 格子悬停气泡：一格 = 当日入库数 */
  'famHeat.tip.day': '{date} · 入库 {count} 款',

  /* ── 图例与口径 ── */
  'famHeat.legend.low': '少',
  'famHeat.legend.high': '多',
  'famHeat.note':
    '口径：一格 = 当日入库（Steam rt_time_acquired）数量，一列 = 一周（周一 → 周日），颜色深浅为相对峰值分档。跨度自适应：首条入库 → 今天，完整保留历史。',
} as const

export default famHeat
