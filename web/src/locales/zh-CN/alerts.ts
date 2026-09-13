/* alerts 词条 —— 价格提醒页（views/alerts/Index.vue）。

   分节 key（`section.*`）**同时是区块标题**：对应区块的 `data-section` 直接写
   这个 key，HlSectionRail 读到后用 t() 显示（同 rates 的约定）。
   其中 `alerts.section.rules` 还是 **ProductTour 引导步骤的选择器契约**——
   视图的 data-section 与 ProductTour 的 target 必须逐字相同，改名即静默落空。

   ⚠️ SMTP 一节里没有任何地址 / 账号字面量：`{host}` 与 `{email}` 是**示例值**，
   由视图侧传入（词典只留句式），`{masked}` 是服务端返回的掩码串，同样只走参数。
   这样「连接信息不进词条」在结构上成立，而不是靠人记得别写。 */

const alerts = {
  /* ── 分节锚点（兼区块标题；alerts.section.rules 为 ProductTour 契约 key）── */
  'alerts.section.rules': '添加提醒规则',
  'alerts.section.list': '提醒规则',
  'alerts.section.history': '触发历史',
  'alerts.section.smtp': '邮件通知设置',

  /* ── 添加规则：搜索 ── */
  'alerts.rules.desc':
    '搜索游戏名称（中英文兼容）或 AppID；优先匹配本地数据库，AppID 无本地记录时在线验证。',
  'alerts.rules.searchPlaceholder': '搜索游戏名称（中文/英文）或 AppID',
  'alerts.rules.search': '搜索',
  'alerts.rules.clear': '清除',
  'alerts.rules.add': '添加',

  /* 目标类型下拉（视图里用 computed 现取，勿改成模块级常量） */
  'alerts.rules.optionPrice': '价格 ≤ 目标',
  'alerts.rules.optionPct': '折扣 ≥ 目标',
  'alerts.rules.optionHistoricLow': '跌破历史最低',

  /* 目标值输入框的占位 / 只读提示 */
  'alerts.rules.hintHistoric': '自动对比历史最低价',
  'alerts.rules.hintPrice': '元（如 50 = ¥50）',
  'alerts.rules.hintPct': '%（如 50 = 5 折以下）',

  /* ── 规则列表 ── */
  'alerts.rules.empty': '暂无规则',
  'alerts.rules.never': '从未',
  'alerts.rules.editTitle': '编辑规则 #{id}',
  'alerts.rules.targetValuePlaceholder': '目标值',
  'alerts.rules.save': '保存',
  /* 删除确认：游戏名（或 AppID 兜底）作为 {name} 整句进词条——
     不做「删除 #」+「规则（」+「）？」的片段拼接，中英括号与语序不同 */
  'alerts.rules.removeConfirm': '删除 #{id} 规则（{name}）？',
  'alerts.rules.confirmTitle': '确认',

  /* 条件列：标签与值合成为一句（「%」随句进词条，不在视图里拼） */
  'alerts.rules.condPrice': '价格 ≤ {value}',
  'alerts.rules.condPct': '折扣 ≥ {value}%',
  'alerts.rules.condHistoricLow': '创新低',

  /* ── 规则表 / 触发历史表共用的表头 ── */
  'alerts.table.game': '游戏',
  'alerts.table.region': '区服',
  'alerts.table.condition': '条件',
  'alerts.table.enabled': '启用',
  'alerts.table.lastTriggered': '上次触发',
  'alerts.table.actions': '操作',
  'alerts.table.time': '时间',
  'alerts.table.priceThen': '当时价格',
  'alerts.table.email': '邮件',

  /* ── 触发历史 ── */
  'alerts.history.empty': '暂无触发记录',
  'alerts.history.notifiedEmail': '已发',
  'alerts.history.notifiedInApp': '站内',

  /* ── 邮件通知设置（SMTP 标签与提示；无任何凭据/连接信息取值）── */
  'alerts.smtp.desc': '配置 SMTP 邮箱后，价格触发提醒时自动发送邮件通知。密码仅存本地数据库。',
  'alerts.smtp.host': 'SMTP 服务器',
  'alerts.smtp.hostPlaceholder': '如 {host}',
  'alerts.smtp.port': '端口',
  'alerts.smtp.user': '发件邮箱',
  'alerts.smtp.userPlaceholder': '如 {email}',
  'alerts.smtp.password': '授权码 / 密码',
  'alerts.smtp.passwordPlaceholder': '输入授权码或密码',
  'alerts.smtp.passwordConfigured': '已配置（{masked}），留空保持不变',
  'alerts.smtp.toAddr': '收件邮箱',
  'alerts.smtp.toAddrPlaceholder': '接收提醒的邮箱地址',
  'alerts.smtp.encryption': '加密方式',
  'alerts.smtp.ssl': 'SSL（465）',
  'alerts.smtp.starttls': 'STARTTLS（587）',
  'alerts.smtp.test': '测试连通性',
  'alerts.smtp.save': '保存邮件设置',

  /* ── 操作反馈（message.*）── */
  'alerts.toast.selectGameFirst': '请先搜索并选择游戏',
  'alerts.toast.enterTarget': '请输入目标值',
  'alerts.toast.added': '规则已添加，爬取落库后自动检查',
  'alerts.toast.enabled': '规则已启用',
  'alerts.toast.disabled': '规则已停用',
  'alerts.toast.updated': '规则已更新',
  'alerts.toast.removed': '已删除规则 #{id}',
  'alerts.toast.smtpSaved': '邮件设置已保存',
  'alerts.toast.testSent': '测试邮件已发送到 {to}，请查收',
} as const

export default alerts
