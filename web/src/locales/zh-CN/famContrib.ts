/* famContrib 词条 —— 家庭组·贡献分布页签（views/family/tabs/FamContrib.vue）。

   术语沿用 A / B 表（英文侧逐字对齐，见 en/famContrib.ts）：
   · 贡献分布 → contribution split
   · 独占 → Exclusive、N人共享 → Shared by {n}、档位/分层 → tier
   · 入库 → acquisition / acquired、占比 → share
   · 家庭库 → family library（与 shared library 不是一个集合）

   三处刻意的写法：
   · 档位标签（`tierLabel`）与档位提示（`tierTip`）分开成两组 key，而不是把
     `tierLabel()` 的结果塞进提示的占位符——那等于拼接两段已翻译的文本，英文
     语序拼不回来；
   · 「N人共享」的 `{n}` 是**人数**（档位层数），提示里的 `{count}` 是**游戏数**，
     两个占位符不可互换；
   · 月份轴标签（原 `${dd.getMonth() + 1}月`）不是词条，走
     `useLocaleFormat().month()`——中文 `1月`、英文 `Jan`。 */
const famContrib = {
  /* ── 空态 ── */
  'famContrib.empty.loading': '家庭库数据拉取中…',
  'famContrib.empty.noData': '暂无家庭库数据——同步家庭组后展示贡献分布',

  /* ── 成员名回退（personaName 为空时用 steamid 末四位）── */
  'famContrib.memberFallback': '成员{id}',

  /* ── 统计范围切换（累计总量 / 近半年）── */
  'famContrib.range.all': '累计总量',
  'famContrib.range.allTip': '统计全部历史入库游戏',
  'famContrib.range.halfYear': '近半年',
  'famContrib.range.halfYearTip': '仅统计近 6 个月入库游戏',

  /* ── 成员贡献堆叠 ── */
  'famContrib.chart.title': '成员贡献堆叠（按共享档位分层）',
  'famContrib.tier.exclusive': '独占',
  'famContrib.tier.shared': '{n}人共享',
  'famContrib.tier.tipExclusive': '独占：{count} 款',
  'famContrib.tier.tipShared': '{members}人共享：{count} 款',

  /* ── 成员贡献占比（环形图）── */
  'famContrib.donut.title': '成员贡献占比',
  'famContrib.donut.total': '入库总数',

  /* ── 近半年入库增量（月度柱）── */
  'famContrib.halfYear.title': '近半年入库增量',
  'famContrib.halfYear.tip': '{month}：{count} 款',

  /* ── 4 张 KPI 卡 ── */
  'famContrib.kpi.total': '家庭库游戏总数',
  /* D4：本页的独占贡献随范围控件变化（筛选口径），与 FamLib 的全库口径是两个
     数；中文侧保持原样，差异体现在英文上（Exclusive contributions (filtered)）。 */
  'famContrib.kpi.exclusive': '独占贡献合计（款）',
  'famContrib.kpi.perMember': '人均独占贡献',
  'famContrib.kpi.rate': '独占率',
} as const

export default famContrib
