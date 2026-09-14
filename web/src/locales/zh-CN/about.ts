/* about 词条 —— 项目说明页（views/about/Index.vue）。

   ⚠️ 本页最容易踩「模块级常量把语言冻死」的坑：FEATURES / CREDITS 是模块级
   数组，在模块加载时求值一次。故它们**只存词条 key**（titleKey / descKey /
   noteKey），模板里 t() 现取；不要把 t() 的结果存进常量表。

   · 功能特性与侧边栏模块一一对应，英文只求简洁顺口，不逐字直译。
   · 技术栈 / 数据来源是「<b>类别</b> — 说明」两段式：类别是术语（label），
     说明是一句话（desc），两条词条 + 模板里的破折号（不是中文，留在组件侧）。
   · 致谢条目只有**说明**需要翻译：项目名与外链是专名，留在组件侧。
   · hero.desc 是整段介绍，英文是重写的通顺句子，不是逐字对译。 */

const about = {
  /* 定位（hero 段落） */
  'about.hero.desc':
    '单机自部署的个人终端——把 Steam 多区价格、愿望单监控、家庭共享库、消费账单与代理链路收进一个本地仪表盘。所有数据只存本机，出网走自有代理链路，不经过任何第三方服务器。',

  /* 运行时长（页脚「已运行」后的值，由秒数分档） */
  'about.uptime.seconds': '{n} 秒',
  'about.uptime.minutes': '{n} 分钟',
  'about.uptime.hours': '{h} 小时 {m} 分',
  'about.uptime.days': '{d} 天 {h} 小时',

  /* 章节标题 */
  'about.section.features': '功能特性',
  'about.section.tech': '技术栈',
  'about.section.sources': '数据来源',
  'about.section.privacy': '隐私与免责',
  'about.section.credits': '致谢',

  /* hero 项目名旁的 GitHub 仓库链接（外链专名，label 仅作可读名） */
  'about.githubRepo': 'GitHub 仓库',

  /* hero logo = 新手引导重看入口（点击打开浮层；首次启动仍由外壳自动弹出） */
  'about.logo.tourTitle': '重新查看新手引导',

  /* 功能特性卡（FEATURES 常量表，与侧边栏模块一一对应） */
  'about.feature.multiRegion.title': '多区价格矩阵',
  'about.feature.multiRegion.desc':
    '41 区服实时价格 / 史低 / 折扣追踪，按汇率自动折算人民币横向对比',
  'about.feature.wishlist.title': '监控池',
  'about.feature.wishlist.desc': '多账户愿望单与已购追踪入池、区服圈定，15 分钟账户同步 + 每 6 小时价格爬取',
  'about.feature.alerts.title': '价格提醒',
  'about.feature.alerts.desc': '降价与历史新低邮件推送，新低优先展示号主所在区',
  'about.feature.rates.title': '汇率追踪',
  'about.feature.rates.desc': '多币种汇率走势图表，卡商充值价目作实付成本参考',
  'about.feature.bundles.title': '捆绑包',
  'about.feature.bundles.desc': 'bundle / sub 双轨身份解析，跨区捆绑包价格与导入队列',
  'about.feature.family.title': '我的家庭组',
  'about.feature.family.desc': '家庭共享库聚合分析：贡献分布、入库热力、价值洞察、成员动态',
  'about.feature.bills.title': '完整账单',
  'about.feature.bills.desc': '多币种消费记录按入账日汇率统一折算人民币',
  'about.feature.proxies.title': '代理管理',
  'about.feature.proxies.desc': 'Clash 订阅接入、节点真实 Steam 连通性体检、冷却生命周期管理',
  'about.feature.tasks.title': '任务中心',
  'about.feature.tasks.desc': '爬虫队列可视化、批量 AppID 导入、三榜单发现源自动补库',
  'about.feature.toolbox.title': '工具箱',
  'about.feature.toolbox.desc': 'CDK 批量激活等实用小工具',

  /* 技术栈（label = <b> 里的类别，desc = 破折号后的说明） */
  'about.tech.frontend.label': '前端',
  'about.tech.frontend.desc':
    'Vue 3 + TypeScript + Vite + Pinia + ECharts，自制 Hl* 组件体系（深 / 浅双主题）',
  'about.tech.backend.label': '后端',
  'about.tech.backend.desc': 'Python FastAPI + SQLAlchemy（async）+ SQLite',
  'about.tech.desktop.label': '桌面',
  'about.tech.desktop.desc': 'pywebview（WebView2）一键启动与登录回传',
  'about.tech.proxy.label': '代理链',
  'about.tech.proxy.desc': 'mihomo（Clash 内核）+ 代理策略引擎（proxy_first）',

  /* 数据来源 */
  'about.source.price.label': '价格',
  'about.source.price.desc': 'Steam 商店页多区爬取（41 区轮转，打折预检省配额、429 退避）',
  'about.source.account.label': '账户',
  'about.source.account.desc': 'Steam 官方 Web API（Cookie 的 JWT 免 Key 通道）',
  'about.source.network.label': '出网',
  'about.source.network.desc': '一律经代理策略引擎（proxy_first），直连被墙域自动走代理',
  'about.source.flags.label': '旗帜',
  /* 尾部的「；」绑定同一行里的下一对（旗帜 … ；汇率 …）——标点随语言走，
     故留在词条里，不在模板里写全角分号（英文界面下会很难看）。 */
  'about.source.flags.desc': '内置素材；',
  'about.source.rates.label': '汇率',
  'about.source.rates.desc': '第三方汇率接口',

  /* 隐私与免责 */
  'about.privacy.localOnly':
    '全部数据（Cookie、账单、价格库、家庭库）只存本机 SQLite，不经过任何第三方服务器',
  'about.privacy.noBypass': '不绕过 Steam 任何隐私限制；他人资料需本人设为公开方可读取',
  'about.privacy.disclaimer': '价格与统计结果仅供参考，购买决策请以 Steam 商店实际展示为准',
  'about.privacy.personalUse': '仅供个人学习与自用，不做任何商业用途',

  /* 致谢（CREDITS 常量表；项目名与外链留在组件侧） */
  'about.credit.mihomo': 'Clash 代理内核（订阅接入与节点管理）',

  /* 运行环境页脚（/system/info 实时） */
  'about.env.version': '版本 v{v}',
  'about.env.dataDir': '数据 {path}',
  'about.env.uptime': '已运行 {d}',
  'about.env.disconnected': '后端未连接——运行环境信息暂缺',
} as const

export default about
