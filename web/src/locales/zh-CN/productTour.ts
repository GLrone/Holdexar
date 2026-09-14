/* ════════════════════════════════════════════════════════════════════
   productTour 词条 —— 新手引导浮层（components/ProductTour.vue）。

   **冻结陷阱（本文件存在的理由）**：ProductTour 的步骤表 `TOUR` 是写在
   `<script setup>` 里的一张常量表，如果在表里直接存 `title: '欢迎来到…'`
   这样的译文，字符串会在组件创建那一刻求值一次——切语言时引导文案原地不动
   （`t()` 的响应式只在**渲染期**调用才成立）。所以表里只存 **key**
   （`titleKey` / `textKey` / `emphKey` / `hintKey`），渲染期再 `t(...)`。
   `scripts/check-i18n.mjs` 的判据 ③ 专门盯这类 `xxxKey: 'a.b.c'` 常量，
   拼错同样会 exit 1。

   `{app}` 是品牌名参数，值由组件侧从 `@/appInfo` 的 `APP_NAME` 传入
   （表里存的是常量引用，不是译文，故不冻结）——品牌名改名时只动 appInfo。

   ⚠️ ProductTour 的 `target` 选择器里那四个 `data-section` 值是**契约**、
   不是文案：`proxies.section.clash` / `settings.section.steamAccount` /
   `crawl.section.bulkImport` / `alerts.section.rules`。它们既不出现在本文件，
   也不该建词条——DOM 锚点与语言无关，建了反而多一个会漂移的来源。
   四个视图侧写的是同一串字符串（见 .tmp-i18n-brief5.md 第一节）。

   引导里点名的界面标签（英文侧逐字对齐的出处）：
   · 代理管理 → nav.proxies · 我 → nav.me · 任务 → nav.crawl · 价格提醒 → nav.alerts
     · 监控池 → nav.pool · 游戏商店 → nav.library · 汇率 → nav.rates
     · 工具箱 → nav.toolbox
   · 保存订阅 → proxies.sub.save · 自动下载内核 → proxies.kernel.autoDownload
     · 启动 → proxies.clash.start · 检测节点（Steam 连通） → proxies.clash.testNodes
     · 代理节点列表 → proxies.section.nodes
     · 添加 Clash 订阅链接 → proxies.sub.clashPlaceholder（引导引其前缀）
   · 登录并自动获取 → settings.steam.autoFetch
     · 如何获取 Cookie → settings.steam.guideSummary（引导引其前缀）
     · 新手教程 → settings.section.tour
   · 导入监控 → crawl.bulk.import
   · 搜索 → alerts.rules.search · 添加 → alerts.rules.add
     · 价格 ≤ 目标 / 折扣 ≥ 目标 / 跌破历史最低 → alerts.rules.optionPrice
       / optionPct / optionHistoricLow
     · 邮件通知设置 → alerts.section.smtp · 触发历史 → alerts.section.history
   引导底部的「上一步 / 下一步 / 完成」复用 common.prev / common.next
   / common.finish（与 HlStepper 同词条，非本模块自有文案）。
   ════════════════════════════════════════════════════════════════════ */

const productTour = {
  /* ── 开场卡（无聚光靶点）── */
  'productTour.intro.title': '欢迎来到 {app}',
  'productTour.intro.p1':
    '这是一款 Steam 多区比价工具：绑定 Steam 账号后，自动跟踪愿望单和已购游戏在全球 40 多个区的价格，降价第一时间提醒你。',
  'productTour.intro.p2':
    '接下来带你把最核心的事走一遍。每一步都会高亮界面上的具体位置，跟着「下一步」走即可。',
  'productTour.intro.p3':
    '中途想离开，点「跳过导览」随时可以——到「关于」页点 {app} logo 能重新打开本导览。',

  /* ── 第 1 步 · 侧栏「代理管理」── */
  'productTour.stepProxy.title': '第 1 步 · 配置代理（一切的前提）',
  'productTour.stepProxy.emph': '不配代理，后面的账号登录窗都加载不出来，价格爬取也会经常失败。',
  'productTour.stepProxy.p1': '国内直连 Steam 商店域名多数受限，所以代理是第 1 步。',
  'productTour.stepProxy.p2':
    '先认识位置：左边高亮的就是「代理管理」（显示器图标）。点「下一步」自动带你跳过去。',

  /* ── 第 2 步 · Clash 接入 ── */
  'productTour.stepClash.title': '第 2 步 · 导入 Clash 订阅',
  'productTour.stepClash.p1':
    '① 把机场订阅链接粘贴到「添加 Clash 订阅链接」输入框（就是平时填在 Clash 客户端里的那条 https:// 链接，仅存本机）；',
  'productTour.stepClash.p2': '② 点「保存订阅」——提示缺内核时点「自动下载内核」，等它装好即可；',
  'productTour.stepClash.p3':
    '③ 点「启动」拉起内置 Clash，系统自动检测哪些节点能真正连通 Steam（能通的才用于抓取）；',
  'productTour.stepClash.p4':
    '④ 之后可随时点「检测节点（Steam 连通）」复检，表格「Steam」列打勾的节点就是可用的。',
  'productTour.stepClash.hint':
    '没有机场订阅？本页下方「代理节点列表」支持手动添加单个节点或批量导入；暂时不配代理也可以继续浏览，但部分功能会缺数据。',

  /* ── 第 3 步 · 侧栏「我」── */
  'productTour.stepAccount.title': '第 3 步 · 前往账号绑定',
  'productTour.stepAccount.p1':
    '代理就绪后，来绑定 Steam 账号。左边高亮的是「我」（人像图标）——绑定入口在这个页面里。点「下一步」自动跳转。',

  /* ── 第 4 步 · Steam 账户绑定 ── */
  'productTour.stepBind.title': '第 4 步 · 绑定 Steam 账号',
  'productTour.stepBind.emph':
    '这是功能的地基：绑定后自动获取钱包余额（右上角胶囊显示）、愿望单、已购游戏库。',
  'productTour.stepBind.p1': '① 点高亮卡片里的「登录并自动获取」按钮（带闪电图标）；',
  'productTour.stepBind.p2':
    '② 弹出 Steam 官方登录窗口（走刚配好的代理），输入账号密码，再按手机 Steam App 的提示确认（Steam Guard 验证）；',
  'productTour.stepBind.p3': '③ 验证通过后窗口自动关闭、登录凭据自动回传——全程不用手动复制。',
  'productTour.stepBind.hint':
    '浏览器环境（非桌面端）没有自动获取按钮：点开本卡片「如何获取 Cookie」折叠项，里面有手动复制的 4 步引导。',

  /* ── 第 5 步 · 侧栏「任务」── */
  'productTour.stepTasks.title': '第 5 步 · 前往任务页',
  'productTour.stepTasks.p1':
    '左边高亮的是「任务」（循环箭头图标）——手动爬取、导入游戏都在这页（区服管理在「监控池」页）。点「下一步」自动跳转。',

  /* ── 第 6 步 · 批量导入 ── */
  'productTour.stepImport.title': '第 6 步 · 添加要监控的游戏',
  'productTour.stepImport.p1': '最简单的入门方式——把想比价的游戏直接粘贴进来：',
  'productTour.stepImport.p2':
    '① 在 Steam 商店（store.steampowered.com）打开某款游戏，复制浏览器地址栏整条链接；',
  'productTour.stepImport.p3':
    '② 粘贴到高亮的批量导入框（一次可粘多条，一行一个，也支持 SteamDB 链接或直接填 AppID 数字）；',
  'productTour.stepImport.emph':
    '③ 点「导入监控」——识别出 AppID 后立即首次抓价入库，作为监控数据保存（不进愿望单与监控池）。',
  'productTour.stepImport.hint':
    '绑定过账号的话，愿望单和已购游戏其实已经自动进监控池（见「监控池」页）；手动导入只作监控数据——想持续盯价的游戏，到游戏卡点星标关注。',

  /* ── 第 7 步 · 侧栏「价格提醒」── */
  'productTour.stepAlerts.title': '第 7 步 · 前往价格提醒页',
  'productTour.stepAlerts.p1':
    '左边高亮的是「价格提醒」（铃铛图标）——设置"降到多少才通知我"。点「下一步」自动跳转。',

  /* ── 第 8 步 · 添加提醒规则 ── */
  'productTour.stepRules.title': '第 8 步 · 设置降价提醒',
  'productTour.stepRules.p1':
    '① 在高亮的搜索框输入游戏名（中英文都行），点「搜索」，在结果里点选目标游戏；',
  'productTour.stepRules.p2':
    '② 选区服（默认你的结算区）和条件：「价格 ≤ 目标」填一个你愿意出手的价（如 39.9）；「折扣 ≥ 目标」填折扣（如 75 表示 75 折）；「跌破历史最低」不用填数字；',
  'productTour.stepRules.emph':
    '③ 点「添加」完成。价格每轮更新都会核对规则，达标自动触发通知。',
  'productTour.stepRules.p3':
    '通知方式：默认应用内通知；同页底部「邮件通知设置」配好 SMTP 后可收邮件。',
  'productTour.stepRules.hint': '触发过的记录在同页「触发历史」分节随时可查。',

  /* ── 收尾卡（无聚光靶点）── */
  'productTour.done.title': '完成！核心链路已走通',
  'productTour.done.emph': '回顾这条链路：配代理 → 绑定账号 → 导入游戏 → 设提醒。',
  'productTour.done.p1': '之后的事全自动：价格每 6 小时刷新、达标即通知、汇率自动同步。',
  'productTour.done.p2':
    '其他页面按需探索：「游戏商店」看全量资产和各区比价、「汇率」看多币种走势、「工具箱」有 CDK 批量激活等实用工具。',
  'productTour.done.p3': '需要重看：到「关于」页点 {app} logo，或到「我」页「新手教程」分节。',

  /* ── 浮层自身的控件 ── */
  'productTour.action.skip': '跳过导览',
  /* 步骤进度（气泡左上角胶囊）。分隔符 `/` 是中性符号，整条进词条不拼接。 */
  'productTour.progress': '{current} / {total}',
} as const

export default productTour
