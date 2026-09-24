/* settings 词条 —— 系统设置页（views/settings/Index.vue）。

   分节 key（`section.*`）同时是**页内分节锚点的显示名**：对应卡片的
   `data-section` 直接写这个 key（锚点与语言无关，切语言时 ProductTour 的
   选择器不会断），HlSectionRail 读到后用 t() 显示。

   ⚠️ `settings.section.steamAccount` 是**契约值**——ProductTour 的
   `target: '[data-section="settings.section.steamAccount"]'` 与视图侧的
   data-section 必须逐字一致，改名等于静默掐断产品引导的第一步。
   另四条 section.* 无契约约束，但同样「一处定义两处消费」（卡片标题 +
   锚点标签），改一条两处同步。

   三处刻意的**整句化**（不是逐片段拼）：
   · 绑定 / 备份 / 更新 / 解绑的结果提示都是一条带占位符的整句——中英
     语序与量词不同（「已解绑 X」对 'Unbound X'、「共 N 份」对
     '{n} backups'），拆成「前缀 + 值 + 后缀」拼不回去。
   · Cookie 分步引导的第 2–4 步在原文里被 <kbd> / <code> / <b> 行内元素
     切碎，这里**整步一条词条**，行内标签写在值里、由组件侧 v-html 渲染
     （同 bundles.calc.excludeHint 的先例）。按元素边界切成片段的写法会
     逼译文把被强调的词钉死在原位，英文拼出来是残句。词条是应用自有静态
     文案（非用户输入），v-html 无注入面。
   · 「当前账号同步时间 + 钱包轮转说明」原文是两个文本节点，合并成一条。

   `update.*Post` 两条**带前导空格**（拼在链接节点之后：`发布页 手动下载。`），
   同 bundles.gameTag.* 的写法，不是笔误：模板里的空白节点在 `</a>` 与
   `{{ }}` 之间不会被保留，英文需要那个空格。 */

const settings = {
  /* ── 分节锚点（data-section 属性值）+ 卡片标题 ── */
  'settings.section.steamAccount': 'Steam 账户绑定',
  'settings.section.account': '账户',
  'settings.section.backup': '数据备份',
  'settings.section.update': '应用更新',
  'settings.section.tour': '新手教程',

  /* ── Steam 账户绑定卡片 ── */
  'settings.steam.desc': '绑定后展示钱包余额（右上角）与账号结算币种/地区。Cookie 仅保留 sessionid / steamCountry / steamLoginSecure 三项，明文只存本机数据库。',
  'settings.steam.cookiePlaceholder': '粘贴含 steamLoginSecure 的 Cookie（支持整行 / 换行格式，自动整理）',
  'settings.steam.cookiePlaceholderBound': '当前账号已绑定（{identity}），粘贴其他账号可新增绑定',
  'settings.steam.autoFetch': '登录并自动获取',
  'settings.steam.bind': '绑定',
  'settings.steam.addAccount': '添加账号',
  'settings.steam.refreshBalance': '刷新余额',

  /* Cookie 获取引导（第 2–4 步整步一条，行内标签写在值里） */
  'settings.steam.guideSummary': '如何获取 Cookie？（推荐点上方「登录并自动获取」，或点开手动分步引导）',
  'settings.steam.guideStep1Pre': '在常用浏览器（Edge / Chrome）打开',
  'settings.steam.guideStep1Post': '并登录 Steam 账号。',
  'settings.steam.guideStep2': '按 <kbd>F12</kbd> 打开开发者工具，切到 <b>网络（Network）</b> 标签，按 <kbd>F5</kbd> 刷新页面。',
  'settings.steam.guideStep3': '点击列表中<b>第一条请求</b>（通常是商店页本身），在右侧「请求标头（Request Headers）」里找到 <code>Cookie:</code> 开头的一整行，<b>右键 → 复制值</b>（很长，必须整行复制）。',
  'settings.steam.guideStep4': '回到本页粘贴到上方输入框点「绑定」。整行带 <code>Cookie:</code> 前缀、或从 Application → Cookies 里逐条复制的换行格式都能自动识别整理。',
  'settings.steam.guideNotes': '只需 sessionid / steamCountry / steamLoginSecure 三项（保存时自动收窄，其余丢弃）；Cookie 明文只存本机数据库。浏览器退出 Steam 登录后绑定会失效，需重新绑定。',

  /* 绑定后的多账号列表 */
  'settings.steam.mismatchWarn': '当前账号的 SteamID 与上方保存的 SteamID64 不一致，请核对是否同一账号',
  'settings.steam.syncOk': '同步正常',
  'settings.steam.syncFail': '同步失败',
  'settings.steam.syncIdle': '未同步',
  'settings.steam.syncTipOk': '上次同步：{time}',
  'settings.steam.syncTipFail': '上次同步失败：{time} · {error}',
  'settings.steam.syncTipIdle': '尚未同步过钱包数据',
  'settings.steam.noNickname': '（未同步昵称）',
  'settings.steam.primary': '主账号',
  'settings.steam.active': '当前',
  'settings.steam.friendCode': '好友码 {code}',
  'settings.steam.friendCodeUnknown': '好友码未知',
  'settings.steam.unknown': '未知',
  'settings.steam.balance': '余额',
  'settings.steam.currency': '币种',
  'settings.steam.region': '地区',
  'settings.steam.games': '游戏',
  'settings.steam.wishlist': '愿望单',
  'settings.steam.redeems': '激活',
  'settings.steam.setActive': '设为当前',
  'settings.steam.unbind': '解绑',
  'settings.steam.syncMeta': '当前账号同步时间：{time} · 钱包每分钟自动轮转刷新（多账号随机错峰）',

  /* ── 账户卡片（SteamID64 / Web API Key）── */
  'settings.account.desc': '监控池同步（愿望单 / 已购）所用的 Steam 身份信息。',
  'settings.account.steamIdPlaceholder': '例如 76561198000000000',
  'settings.account.lookup': '查询',
  'settings.account.applyFree': '免费申请',
  'settings.account.apiKeyHint': '已购游戏同步需要 · 明文仅存本机数据库',
  'settings.account.apiKeyPlaceholder': '输入 API Key',
  'settings.account.apiKeyPlaceholderSet': '已配置（{mask}），留空则保持不变',
  'settings.account.save': '保存',

  /* ── 数据备份卡片 ── */
  'settings.backup.desc': '在线快照备份：不占用数据库文件、不打断爬取写入，完整包含所有已提交数据；每天自动备份一次，保留最近 5 份。恢复为危险操作——将用备份文件整体替换当前数据库，执行前请先校验。',
  'settings.backup.createNow': '立即备份',
  'settings.backup.snapshotting': '快照中…',
  'settings.backup.count': '共 {n} 份',
  'settings.backup.verify': '校验',
  'settings.backup.verifying': '校验中…',
  'settings.backup.download': '下载',
  'settings.backup.restore': '恢复',
  'settings.backup.restoring': '恢复中…',
  'settings.backup.confirmRestore': '确认恢复？',
  'settings.backup.remove': '删除',
  'settings.backup.removing': '删除中…',
  'settings.backup.empty': '尚无备份——点击「立即备份」创建第一份。',

  /* ── 应用更新卡片 ── */
  'settings.update.desc': '读取发布清单比对版本，下载并校验 SHA256 后重启应用即可完成更新。更新只替换程序文件，你的数据（游戏、价格、账号、备份）保存在独立的数据目录中，永不受影响。',
  'settings.update.check': '检查更新',
  'settings.update.checking': '检查中…',
  'settings.update.currentVersion': '当前版本 v{version}',
  'settings.update.pendingReady': 'v{version} 已下载并校验完成，重启应用即可完成更新。',
  'settings.update.restartNow': '重启并更新',
  'settings.update.later': '暂不更新',
  'settings.update.download': '下载 v{version}',
  'settings.update.downloading': '下载中…',
  'settings.update.noChecksum': '（本次发布未提供校验值）',
  'settings.update.networkPre': 'GitHub 暂时不可达，可稍后重试或前往',
  'settings.update.noReleasePre': '暂未查到可用的发布版本，可稍后重试或前往',
  'settings.update.releasesPage': '发布页',
  'settings.update.networkPost': '手动下载。',
  'settings.update.noReleasePost': '查看。',
  'settings.update.upToDate': '已是最新版本（v{version}）。',
  'settings.update.availableHint': '发现新版本 v{version}，点「检查更新」在弹窗里一键下载安装。',
  'settings.update.phaseDownloading': '下载中 {progress}',
  'settings.update.phaseVerifying': '校验 SHA256…',
  'settings.update.phaseExtracting': '解包中…',
  'settings.update.phaseProcessing': '处理中…',
  /* 更新行为开关（提示 / 静默自动更新）：标签 + 说明 + 落定提示各一条 */
  'settings.update.notifyLabel': '新版本提示',
  'settings.update.notifyHint':
    '发现新版本时弹窗提醒，并在侧栏「我」上亮红点。关闭后不再主动打扰，仍可随时手动检查。',
  'settings.update.notifyOn': '已开启新版本提示',
  'settings.update.notifyOff': '已关闭新版本提示，不再主动打扰',
  'settings.update.autoLabel': '静默自动更新',
  'settings.update.autoHint':
    '发现新版本后在后台自动下载并校验，不弹窗；下次启动应用时自动完成更新。',
  'settings.update.autoOn': '已开启静默自动更新，将在后台自动下载',
  'settings.update.autoOff': '已关闭静默自动更新，改为手动下载',
  'settings.update.autoStarted': '已开始后台下载 v{version}，完成前不会打扰你',
  'settings.update.switchFailed': '设置保存失败，请重试',

  /* ── 新手教程卡片（导览正文在 ProductTour.vue，本页只有入口）── */
  'settings.tour.desc': '蒙层聚光式导览：添加游戏 → 看价格 → 设提醒，三步主线走完就能用起来；代理与账号绑定标为可选，可整个跳过。首次启动已自动展示过，可随时重新查看。',
  'settings.tour.replay': '重新查看导览',

  /* 工具箱次级入口（工具箱已移出一级导航，设置页是其常驻入口） */
  'settings.section.toolbox': '工具箱',
  'settings.toolbox.desc': '账单消费摘要与 CDK 批量激活等辅助能力。',
  'settings.toolbox.open': '打开工具箱',

  /* ── 结果提示（一条整句；中英语序不同，不拆片段）── */
  'settings.toast.accountSwitched': '已切换当前账号',
  'settings.toast.accountUnbound': '已解绑 {name}',
  'settings.toast.backupCreated': '备份完成：{name}（{size}，{games} 款游戏，校验通过）',
  'settings.toast.backupVerified': '校验通过：{name}（{games} 款游戏）',
  'settings.toast.backupVerifyFailed': '校验失败：{name} 文件不自洽，勿用于恢复',
  'settings.toast.backupRestored': '已从 {name} 恢复，数据已生效',
  'settings.toast.backupRemoved': '已删除 {name}',
  'settings.toast.updateCheckFailed': '更新检查失败：GitHub 暂不可达（可稍后重试或手动下载）',
  'settings.toast.updateDownloaded': '新版本已下载并校验完成，重启应用即可完成更新',
  'settings.toast.updateDownloadFailed': '下载失败：{error}',
  'settings.toast.updateCancelled': '已放弃本次更新，暂存已清除',
  'settings.toast.restartFailed': '重启失败，请手动重启应用',
  'settings.toast.restartUnsupported': '浏览器模式不支持自动重启，请手动重启应用',
  'settings.toast.restartError': '重启调用失败，请手动重启应用',
  'settings.toast.accountSaved': '账户信息已保存',
  'settings.toast.cookieMismatch': 'Cookie 与已保存 SteamID64 不一致',
  'settings.toast.bindSuccess': '绑定成功，钱包余额 {balance}',
  'settings.toast.bindSyncFailed': 'Cookie 已保存，但抓取余额失败：{error}',
  'settings.toast.cookieSaved': 'Cookie 已保存',
  'settings.toast.cookieEmpty': '请先粘贴 Cookie',
  'settings.toast.desktopOnly': '自动获取仅桌面窗口模式可用；浏览器环境请按下方引导手动粘贴',
  'settings.toast.loginOpened': '已打开 Steam 登录窗口，登录成功后会自动返回…',
  'settings.toast.rebindDetected': '检测到已绑定账号 {id}，正在换绑…',
  'settings.toast.accountRecognized': '识别到账号 {id}',
  'settings.toast.cookieFetchFailed': '未获取到 Cookie',
  'settings.toast.fetching': '获取中…',
  'settings.toast.walletRefreshed': '已刷新',
  'settings.toast.walletRefreshFailed': '刷新失败',
  // ── 绑定风险弹窗（每次绑定动作必弹；确定键 5s 倒计时）──
  // 条目成对存 key：Lead = 加粗关键词，Rest = 短说明（差异化排版，见 Index.vue RISK_ITEMS）
  'settings.risk.title': '绑定 Steam 账户前必读',
  'settings.risk.bodyTitle': '风险须知',
  'settings.risk.item1Lead': '凭据明文保存',
  'settings.risk.item1Rest': 'Steam 登录 Cookie 明文存进本机数据库，等同把账号交给本应用。',
  'settings.risk.item2Lead': '仅限本机使用',
  'settings.risk.item2Rest': '凭据不上传任何服务器，只服务钱包、愿望单、库同步等本地功能。',
  'settings.risk.item3Lead': '本机可被读取',
  'settings.risk.item3Rest': '共用电脑或恶意软件可能拿到这份凭据，请自行评估设备环境。',
  'settings.risk.item4Lead': '介意请用小号',
  'settings.risk.item4Rest': '担心主账号风险，就绑定专门的 Steam 小号。',
  'settings.risk.leakTitle': '如果 Cookie 已泄露',
  'settings.risk.leak1Lead': '立即改 Steam 密码',
  'settings.risk.leak1Rest': '所有旧登录会话随之失效，泄露的 Cookie 作废（含退出所有会话的效果）。',
  'settings.risk.leak2Lead': '开启 Steam Guard',
  'settings.risk.leak2Rest': '确认手机验证器已开启。',
  'settings.risk.leak3Lead': '排查异常记录',
  'settings.risk.leak3Rest': '检查登录、交易与市场记录，发现异常立即联系 Steam 客服。',
  'settings.risk.leak4Lead': '解绑清凭据',
  'settings.risk.leak4Rest': '回设置页「解绑全部账号」，清掉已保存的凭据。',
  'settings.risk.confirm': '我已知晓，继续绑定',
  'settings.risk.countdown': '我已知晓（{n}s）',

  /* ── 通知（价格事件通知：类别是用户面分类，不含内部事件枚举）── */
  'settings.section.notification': '通知',
  'settings.notification.desc':
    '价格刷新产生变化时自动发邮件；默认关闭，开启必须由你确认。',
  'settings.notification.enabledLabel': '启用价格通知',
  'settings.notification.enabledHint':
    '关闭后不产生任何通知候选，也不会积压旧账；重新开启不会补发之前的变动。',
  'settings.notification.categoriesLabel': '通知类别',
  'settings.notification.categoriesHint':
    '事件类型归到这四类中，关掉一类即不再通知该类变化。',
  'settings.notification.quietLabel': '静默时间',
  'settings.notification.quietHint':
    '静默期内产生的变化不会立即发送，会随下一轮一并送达（不是丢弃）。',
  'settings.notification.detailsLabel': '邮件里列出变化明细',
  'settings.notification.smtpInfo': '出口：{host}:{port} · {user}',
  'settings.notification.smtpMissing': '未配置 SMTP，请先到「价格提醒」页填写邮箱与授权码',
  'settings.notification.smtpNoPassword': '未设置授权码',
  'settings.notification.off': '通知未开启',
  'settings.notification.on': '通知已开启，等待下一轮价格刷新',
  'settings.notification.lastOk': '最近一次邮件已送出（{time}）',
  'settings.notification.lastFailed': '最近一次邮件发送失败：{reason}',
  'settings.notification.lastRetryable': '临时故障，下一轮会自动重试（已尝试 {n} 次）',
  'settings.notification.lastPermanent': '已放弃重试，请检查邮箱与授权码',
  'settings.notification.test': '发送测试邮件',
  'settings.notification.testSent': '测试邮件已发送，请到收件箱确认',
  'settings.notification.stats':
    '已投递 {delivered} 条 · 失败 {failed} 条（其中 {retryable} 条可重试）',
} as const

export default settings
