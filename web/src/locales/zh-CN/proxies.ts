/* proxies 词条 —— 代理管理页（views/proxies/Index.vue）。
   页内四区：路由策略卡片网格 / Clash 接入 / 代理节点列表 / 走线控制台。

   分节 key（`section.*`）同时是**页内分节锚点的显示名**：四个区块的
   `data-section` 直接写这个 key（锚点与语言无关，切语言时 ProductTour 的
   选择器不会断），HlSectionRail 读到后用 t() 显示。
   其中 `proxies.section.clash` 是**契约值**——ProductTour 的 target 里逐字
   引用同一个字符串（见 .tmp-i18n-brief5.md 第一节），改名即引导静默落空。

   几处刻意的复用：
   · 两张表格的「节点地址」「状态」列头是同一件事，共用 node.col*；
   · 「改名」「删除」「保存订阅」在订阅行与节点行是同一个动作，共用一份；
   · 「检测中…」在池测试与 Clash 逐节点检测两处是同一句话，共用 node.checking。

   参数化整句（不拆片段拼接）：内核下载/安装结果、订阅重拉、明文订阅导入
   统计、Clash 检测结果——中英语序与量词不同，拆成「标签 + 值」多条再拼
   拼不回去。`kernel.sizeHint` / `kernel.viaWrap` 是**标点差异**（全角括号与
   分隔符）落进词条，让英文界面不出现全角括号。 */

const proxies = {
  /* 分节锚点 + 区块标题（data-section 直接写这组 key） */
  'proxies.section.routing': '路由策略',
  'proxies.section.clash': 'Clash 接入',
  'proxies.section.nodes': '代理节点列表',
  'proxies.section.console': '走线控制台',

  /* Hero */
  'proxies.hero.title': '代理 IP 池管理',
  'proxies.hero.subtitle': '管理出口代理节点，支持路由策略切换、订阅接入、连通性检测与走线日志',

  /* 路由策略卡片（desc 含 <br><small>，走 v-html） */
  'proxies.strategy.proxyFirst.label': '代理优先（推荐）',
  'proxies.strategy.proxyFirst.desc': 'Clash 在跑走 Clash<br><small>否则代理池轮询，最后直连</small>',
  'proxies.strategy.directOnly.label': '直连',
  'proxies.strategy.directOnly.desc': '全部走本机网络<br><small>不使用任何代理</small>',
  'proxies.strategy.directFirst.label': '直连优先',
  'proxies.strategy.directFirst.desc': '本机优先<br><small>失败时换代理重试</small>',
  'proxies.strategy.proxyOnly.label': '完全走代理',
  'proxies.strategy.proxyOnly.desc': '所有请求经代理池<br><small>轮询发出</small>',
  'proxies.strategy.on': '启用',
  'proxies.strategy.off': '关闭',
  'proxies.strategy.updated': '路由策略已更新（下个任务生效）',

  /* 本地混合端口（纯配置，不随策略切换） */
  'proxies.port.label': '本地混合端口',
  'proxies.port.title': '自启 Clash/Verge 的混合端口（代理优先的回落探测指向它）',
  'proxies.port.invalid': '端口须为 1024–65535 的整数',
  'proxies.port.updated': 'Clash 端口已更新',

  /* 内核下载 / 安装 */
  'proxies.kernel.dialogTitle': '正在下载内核',
  'proxies.kernel.phasePrepare': '准备下载',
  'proxies.kernel.sizeHint': '· mihomo 约 15MB',
  'proxies.kernel.viaWrap': '（{via}）',
  'proxies.kernel.downloading': '下载中…',
  'proxies.kernel.autoDownload': '自动安装内核',
  'proxies.kernel.downloadDone': '内核下载完成',
  'proxies.kernel.downloadFailed': '内核下载失败：{error}',
  'proxies.kernel.orPlaceManual': '或手动放置到 {dir}',
  'proxies.kernel.pathHint': '内核路径 {path}',
  'proxies.kernel.installOk': '内核安装成功：{version}',
  'proxies.kernel.installFailed': '安装失败',
  'proxies.kernel.downloadHint': '正在自动下载并安装 Clash 内核，完成后将保存订阅并下载节点…',
  'proxies.kernel.ready': '内核就绪',
  'proxies.kernel.missing': '内核缺失',

  /* Clash 运行状态与启停 */
  'proxies.clash.running': '运行中 · 端口 {port}',
  'proxies.clash.stopped': '未运行',
  'proxies.clash.notRunningTitle': 'Clash 未运行',
  'proxies.clash.start': '启动',
  'proxies.clash.stop': '停止',
  'proxies.clash.started': 'Clash 已启动，混合端口 {port}，正在后台检测节点…',
  'proxies.clash.hasStopped': 'Clash 已停止',
  'proxies.clash.startHint': '启动拉取选中订阅并拉起内核，完成后自动后台检测节点（与手动检测互斥排队）',
  'proxies.clash.needSub': '请先添加 Clash 订阅链接（长期保存在本地库）',
  'proxies.clash.allDeprecated': '全部 Clash 订阅已废弃（不可用节点超过 95%），请手动删除或更换订阅',
  'proxies.clash.switchedToUsable': '选中订阅已废弃，已自动切换到最近一条可用订阅',

  /* Clash 节点检测 */
  'proxies.clash.testNodes': '检测节点（Steam 连通）',
  'proxies.clash.testing': '逐节点检测 Steam 连通性中（冷却期节点自动跳过，请耐心等待）…',
  'proxies.clash.testDone': '检测完成：通 Steam {alive}/{total}',
  'proxies.clash.testDoneDeprecated': '{msg} —— 该订阅不可用节点超过 95%，已标记废弃（后端不再使用，请手动删除或更换）',
  'proxies.clash.aliveTag': '通 Steam {alive}/{total}',
  'proxies.clash.uniqueExits': '去重后 {n} 个出口',
  'proxies.clash.reachable': '通',
  'proxies.clash.unreachable': '不通',
  'proxies.clash.healthy': '健康',
  'proxies.clash.unavailable': '不可用',
  'proxies.clash.cooling': '冷却中',
  'proxies.clash.coolingTitle': '冷却期内跳过检测，沿用上次判定',
  'proxies.clash.sameExit': '同出口',

  /* 订阅（Clash / 明文两种方式共用） */
  'proxies.sub.save': '保存订阅',
  'proxies.sub.edit': '编辑',
  'proxies.sub.editTitle': '编辑订阅',
  'proxies.sub.nameLabel': '订阅名称',
  'proxies.sub.namePlaceholder': '留空则拉取时自动识别（识别结果保存在本地）',
  'proxies.sub.urlLabel': '订阅链接',
  'proxies.sub.urlPlaceholder': 'https://… 订阅链接',
  'proxies.sub.editSave': '保存',
  'proxies.sub.editTip': '修改链接后自动重新拉取（优先直连，直连失败自动改用已保存的可用代理）',
  'proxies.sub.edited': '订阅已更新',
  'proxies.sub.editedSynced': '订阅已更新并重新拉取（{parts}）',
  'proxies.sub.urlInvalid': '订阅链接必须是 http(s) 链接',
  'proxies.sub.savedNodes': '订阅已保存（{nodes} 节点）',
  'proxies.sub.saved': '订阅已保存',
  'proxies.sub.savedPlain': '订阅已保存（长期保留在本地库）',
  'proxies.sub.kernelInstalled': '内核已自动安装',
  'proxies.sub.kernelInstalledVersion': '内核已自动安装{version}',
  'proxies.sub.deleted': '已删除订阅（{name}）',
  'proxies.sub.confirmDelete': '删除该订阅链接？',
  'proxies.sub.confirmDeleteDeprecated': '该订阅已废弃（{reason}），后端不再使用。确认删除？',
  'proxies.sub.deleteDeprecatedTitle': '删除已废弃订阅',
  'proxies.sub.deprecatedReason': '不可用节点超过 95%',
  'proxies.sub.deprecated': '已废弃',
  'proxies.sub.aliveTag': '存活 {alive}/{total}',
  'proxies.sub.syncing': '拉取中…',
  'proxies.sub.refetch': '重拉',
  'proxies.sub.synced': '已重新拉取（{parts}）',
  'proxies.sub.syncedRestarted': '已重新拉取（{parts}），内核已重启，正在后台检测节点…',
  'proxies.sub.nodesCount': '节点 {n}',
  'proxies.sub.trafficUsed': '已用 {size} GB',
  'proxies.sub.emptyClash': '尚无 Clash 订阅 —— 下方添加后长期保存，启动时自动拉取',
  'proxies.sub.allDeprecated': '全部订阅已废弃（不可用节点超过 95%）—— 后端不再使用，请手动删除或更换订阅',
  'proxies.sub.clashPlaceholder': '添加 Clash 订阅链接（https://… 机场订阅，仅存本地）',
  'proxies.sub.failTitle': '保存订阅失败',
  'proxies.sub.failTip': '建议先开启代理（如桌面 Clash Verge / 本应用的 Clash 内核），再点击「重试」重新保存订阅',

  /* 明文代理订阅 */
  'proxies.plain.empty': '尚无代理订阅 —— 添加明文订阅链接（商业爬虫代理协议，非 Clash），一键导入节点池',
  'proxies.plain.placeholder': '添加代理订阅链接（https://… 明文 host:port / scheme://user:pass@host:port）',
  'proxies.plain.import': '导入节点',
  'proxies.plain.importing': '导入中…',
  'proxies.plain.importToast': '正在拉取订阅并导入节点…',
  'proxies.plain.imported': '导入完成：新增 {added} 条，跳过重复 {skipped} 条',
  'proxies.plain.importedChecked': '导入完成：新增 {added} 条，跳过重复 {skipped} 条，实测可用 {alive}/{checked}',
  'proxies.plain.lastImport': '上次导入 +{n}',

  /* 节点池 */
  'proxies.node.summary': '共 {total} 条 · 启用 {enabled}',
  'proxies.node.testAll': '全池测试',
  'proxies.node.testingAll': '测试中…',
  'proxies.node.checking': '检测中…',
  'proxies.node.testAllDone': '全池测试完成',
  'proxies.node.clear': '清空',
  'proxies.node.cleared': '已清空',
  'proxies.node.confirmClear': '清空全部 {n} 条代理？',
  'proxies.node.add': '添加',
  'proxies.node.added': '已添加',
  'proxies.node.addedCount': '已添加 {n} 条代理',
  'proxies.node.manualPlaceholder': '手动添加：host:port 或 scheme://user:pass@host:port（回车确认）',
  'proxies.node.batchImport': '批量导入',
  'proxies.node.batchPlaceholder': '批量导入：每行一条（host:port / host:port:user:pass / scheme://user:pass@host:port）',
  'proxies.node.empty': '暂无代理 —— 保存订阅后一键导入，或手动添加',
  'proxies.node.enabled': '已启用该代理',
  'proxies.node.disabled': '已禁用该代理',
  'proxies.node.deleted': '已删除该代理',
  'proxies.node.confirmDelete': '删除代理 {url}？',
  'proxies.node.test': '测试',
  'proxies.node.testOk': '可用，延迟 {ms}ms',
  'proxies.node.testFailed': '不可用：{error}',
  'proxies.node.unknownError': '未知错误',
  'proxies.node.colAddress': '节点地址',
  'proxies.node.colExitIp': '出口 IP',
  'proxies.node.colStatus': '状态',
  'proxies.node.colDuration': '耗时',
  'proxies.node.colLatency': '延迟',
  'proxies.node.colEnabled': '启用',
  'proxies.node.colActions': '操作',

  /* 走线控制台 */
  'proxies.console.count': '{n} 条',
  'proxies.console.empty': '暂无走线记录',

  /* 生产作业（真实爬取作业台账：**只记事实，不下健康结论**） */
  'proxies.section.jobs': '生产作业',
  'proxies.jobs.desc': '每次真实爬取作业一行；这里只记录事实，不下健康结论。',
  'proxies.jobs.total': '共 {n} 条',
  'proxies.jobs.refresh': '刷新',
  'proxies.jobs.loadFailed': '生产作业记录读取失败',
  'proxies.jobs.empty': '还没有生产作业记录 —— 跑一次爬取后这里会出现',
  'proxies.jobs.extra': '另有：中断 {interrupted} · 进行中 {running}',
  'proxies.jobs.stat.runs': '作业次数',
  'proxies.jobs.stat.success': '成功',
  'proxies.jobs.stat.partial': '部分失败',
  'proxies.jobs.stat.failed': '失败',
  'proxies.jobs.stat.avgDuration': '平均耗时',
  'proxies.jobs.stat.poolExitIp': '最近一次池出口 IP（去重）',
  'proxies.jobs.col.time': '时间',
  'proxies.jobs.col.status': '状态',
  'proxies.jobs.col.tasks': '任务',
  'proxies.jobs.col.success': '成功',
  'proxies.jobs.col.failed': '失败',
  'proxies.jobs.col.duration': '耗时',
  'proxies.jobs.col.node': '节点',
  'proxies.jobs.col.exitIp': '出口 IP',
  'proxies.jobs.status.running': '进行中',
  'proxies.jobs.status.success': '成功',
  'proxies.jobs.status.partial': '部分失败',
  'proxies.jobs.status.failed': '失败',
  'proxies.jobs.status.interrupted': '中断',
  'proxies.jobs.detail.title': '作业 #{id}',
  'proxies.jobs.detail.basic': '本次作业',
  'proxies.jobs.detail.node': '本次节点',
  'proxies.jobs.detail.nodeUnknown': '未记录（读控制器失败或尚未运行）',
  'proxies.jobs.detail.startedAt': '开始',
  'proxies.jobs.detail.finishedAt': '结束',
  'proxies.jobs.detail.duration': '耗时',
  'proxies.jobs.detail.proxyUrl': '池入口',
  'proxies.jobs.detail.poolSha': '池 SHA',
  'proxies.jobs.detail.poolNodes': '池节点',
  'proxies.jobs.detail.poolExitIps': '池出口 IP',
  'proxies.jobs.detail.subscriptions': '来源订阅',
  'proxies.jobs.detail.snapshots': '快照',
  'proxies.jobs.detail.errors': '失败类型',
  'proxies.jobs.detail.errorsNone': '本次没有失败分类记录',
  'proxies.jobs.detail.summaryMissing': '此行没有错误汇总（中断或更早的作业）',
  'proxies.jobs.detail.truncated': '明细已截断',
  'proxies.jobs.detail.interrupted': '这次作业没跑完（进程中断或手动停止）',
  /* 错误分类枚举（与后端 jobruns.ERROR_KINDS 逐项对齐） */
  'proxies.jobs.err.timeout': '超时',
  'proxies.jobs.err.connect': '连接失败',
  'proxies.jobs.err.reset': '连接被重置',
  'proxies.jobs.err.proxy_error': '代理错误',
  'proxies.jobs.err.rate_limit': '限流（429）',
  'proxies.jobs.err.http_4xx': 'HTTP 4xx',
  'proxies.jobs.err.http_5xx': 'HTTP 5xx',
  'proxies.jobs.err.tls': 'TLS 错误',
  'proxies.jobs.err.dns': 'DNS 解析失败',
  'proxies.jobs.err.ledger': '整批重试耗尽',
  'proxies.jobs.err.internal': '内部错误',
  'proxies.jobs.err.other': '其它',

  /* 动作与弹窗（本模块内复用；与 common 无同值项） */
  'proxies.action.delete': '删除',
  'proxies.dialog.confirm': '确认',
} as const

export default proxies
