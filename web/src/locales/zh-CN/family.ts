/* family 词条 —— 家庭组主页（views/family/Index.vue），5 个页签的宿主。
   不变量：family.member.recent30 统计 timeAcquired（入库）非游玩，英文必须是 "Acquired in last 30 days"、禁用 active；
   family.role.family（短 'Family'）与 family.role.familyMember（'family member'）是长短分工非重复；
   family.section.module 同时喂 data-section 锚点与模块标题（锚点与语言无关）；
   带 {占位符} 的词条禁止组件侧拼中文片段；家庭术语英译沿用既有译法，跨模块不各定各的。 */

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

  /* 同步家庭组（多账号逐个发现） */
  'family.action.syncing': '同步中…',
  'family.action.sync': '⟳ 同步家庭组',
  'family.sync.all': '已同步 {n} 个家庭组，成员已自动追踪',
  'family.sync.none': '绑定的账号均未加入 Steam 家庭组',
  'family.sync.partial': '已同步 {joined} 个家庭组；{failed} 个账号同步失败，可稍后重试',
  'family.group.unnamed': '未命名',
  'family.group.notJoined': '该账号未加入任何 Steam 家庭组（官方上限 6 人，可在 Steam 客户端创建或加入）',
  'family.group.notSynced': '该账号尚未同步家庭组——点「同步家庭组」开始',

  /* 成员地区（服务端判定：手动 > 钱包结算区 > 资料国家；未设置不兜底国区） */
  'family.member.regionUnset': '地区未设置',

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
