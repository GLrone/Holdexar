/* family 词条 —— 家庭组主页（views/family/Index.vue）。5 个页签的宿主：
   角色徽章、成员列表、地区弹层、同步/添加成员链路。

   中英对应关系遵循期 6 brief 的 A 表（既有已上线英文，逐字沿用）与 B 表
   （本期新定术语），几个人并行迁 family 时不许各定各的：
   · 家庭组 = Family（`family.role.family`，同 bundles.badge.family）
   · 家庭库 = family library（`family.section.module` / `family.tab.lib`）
     ⚠️ 与「共享库 = shared library」不是同一个集合（D1）
   · 贡献分布 = contribution split / 入库热力图 = acquisition heatmap /
     价值洞察 = value insights（A 表）
   · 成员洞察 = Member insights / 购买动态 = Purchase activity /
     游玩动态 = Play activity（B 表）
   · 独占 = Exclusive / 时长 = playtime（B 表）
   · 全部 = All —— 直接复用 `common.all`，不在这里再写一条

   几处有意的分工，改一条要看清楚：
   · `family.role.family`（徽章，短 'Family'）与 `family.role.familyMember`
     （二级窗叙述，'family member'）是**刻意的长短分工**，不是重复（D8）。
   · `family.member.recent30` 中文写「近30日活跃」，但它统计的是 `timeAcquired`
     （入库）不是游玩——英文必须写 "Acquired in last 30 days"，**绝对不要用
     active**（D6）。中文侧保持原样不动。
   · `family.member.owned` / `.exclusive` / `.recent30` 三条是同一行里的三个
     并列标签，英文保持「标签 + 数」同序，视觉才排得齐。

   带 {占位符} 的词条**不要**在组件侧用 + 或模板字符串拼中文片段：中英语序
   不同，拼不出版行。`family.section.module` 同时喂 data-section 锚点与模块
   标题——锚点必须与语言无关（ProductTour 的 querySelector 才不会静默落空），
   所以属性值写 key、HlSectionRail 读到后 t() 显示（期 5/6 统一改法）。 */

const family = {
  /* 模块头（data-section 锚点 + 模块标题共用一条） */
  'family.section.module': 'Steam 家庭库',
  'family.module.sub': '最多 6 名成员 · 贡献 / 热力图 / 购买动态 / 愿望单',

  /* 页签（键存进 tabs 常量表，渲染期 t() 取） */
  'family.tab.contrib': '贡献分布',
  'family.tab.heat': '入库热力图',
  'family.tab.value': '价值洞察',
  'family.tab.insights': '成员洞察',
  'family.tab.buy': '购买动态',
  'family.tab.play': '游玩动态',
  'family.tab.wish': '家庭愿望单',
  'family.tab.lib': '家庭库',

  /* 角色（D8：徽章用短 Family，二级窗叙述用 family member） */
  'family.role.primary': '主账号',
  'family.role.family': '家庭组',
  'family.role.familyMember': '家庭组成员',

  /* 成员行 */
  'family.member.unnamed': '成员{id}',
  'family.member.friendCode': 'Steam好友码 · {code}',
  'family.member.notLinked': '待绑定',
  'family.member.owned': '库内 {n}',
  'family.member.exclusive': '独占 {n}',
  'family.member.recent30': '近30日活跃 {n}',
  'family.member.remove': '移除',

  /* 地区二级窗 */
  'family.regionPop.who': '{role} · 当前 {region}',
  'family.regionPop.search': '搜索地区（名称 / 代码）…',
  'family.regionPop.empty': '没有匹配的地区',

  /* 同步家庭组 */
  'family.action.syncing': '同步中…',
  'family.action.sync': '⟳ 同步家庭组',
  'family.sync.success': '家庭组「{name}」同步成功，{n} 名成员已自动追踪',
  'family.sync.notJoined': '当前账号未加入家庭组',
  'family.group.unnamed': '未命名',

  /* 添加成员 */
  'family.add.placeholder': '添加成员：Steam 好友码（如 998167239）或 SteamID64，输入即识别',
  'family.add.needInput': '请先输入好友码或 SteamID64（解析出账号后再添加）',
  'family.add.trackFailed': '已加入列表，但入库追踪失败：{err}',
  'family.add.success': '已添加 {name}：已进入监控池追踪（绑定 Cookie 后点「同步家庭组」自动补齐全家）',
  'family.add.confirming': '添加中…',
  'family.add.action': '添加',
  'family.add.invite': '＋ 邀请成员加入家庭组（当前 {n} / 6，还可加入 {left} 人，可拖拽排序）',

  /* 好友码解析预览 */
  'family.resolve.loading': '正在识别账号…',
  'family.resolve.noName': '（未获取到昵称，资料可能为私有）',
  'family.resolve.ok': '识别成功 · 回车或点「添加」',
} as const

export default family
