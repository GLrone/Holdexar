/* ════════════════════════════════════════════════════════════════════
   外壳（app shell）：侧边导航 / 顶栏胶囊 / 头像 / 应用更新。
   这些文案挂在常驻的 App.vue 上，不随路由进出，故单独成模块。
   ════════════════════════════════════════════════════════════════════ */

const shell = {
  /* 路由页标题：router meta.titleKey → document.title（App.vue pageTitle）。
     与上面的 nav.* 同值但**刻意分成同一批 key 复用**——侧边栏条目与页面标题
     本就该同名，两份 key 迟早会漂移。nav.gameDetail 是唯一例外：游戏详情页
     没有侧边栏入口，只为标题而存在。 */
  'nav.gameDetail': '游戏详情',

  /* 侧边导航（App.vue navGroups） */
  'nav.group.overview': '总览',
  'nav.group.assets': '资产库',
  'nav.group.monitor': '监控中心',
  'nav.group.system': '系统',
  'nav.dashboard': '仪表盘',
  'nav.library': '游戏商店',
  'nav.gamelib': '游戏库',
  'nav.achievements': '成就殿堂',
  'nav.bundles': '捆绑包',
  'nav.pool': '监控池',
  'nav.family': '我的家庭组',
  'nav.bills': '账单分析',
  'nav.crawl': '任务',
  'nav.proxies': '代理管理',
  'nav.alerts': '价格提醒',
  'nav.rates': '汇率',
  'nav.toolbox': '工具箱',
  'nav.logs': '运行日志',
  'nav.me': '我',
  'nav.about': '关于',

  /* 顶栏价格更新状态胶囊：一个胶囊只表达一个产品结论，不带数量——内部计数
     的口径是任务批次（1 批 = 1 地区 × ≤400 款），不是游戏数，不得以「款」
     进入用户面；内部 job / 队列 / 速度 / worker / 代理也不进这里（技术诊断
     入口另说） */
  'header.crawl': '等待更新',
  'header.crawlRunning': '正在更新游戏价格',
  'header.crawlDone': '价格已更新',
  'header.crawlPartial': '部分更新',
  'header.crawlTip': '部分地区价格暂未更新，系统会稍后自动重试',
  'header.crawlTipDone': '本次更新已完成',

  /* 顶栏钱包胶囊 + 余额弹层 */
  'wallet.bind': '绑定钱包',
  'wallet.unboundTip': '未绑定 Steam Cookie，点击前往「我」页绑定',
  'wallet.titleMain': '主账号钱包余额：{balance}',
  'wallet.titleMore': '（点击查看全部账号余额）',
  'wallet.titleSynced': ' · 同步于 {time}',
  'wallet.popTitle': '全部账号余额',
  'wallet.popRefresh': '立即刷新当前账号余额',
  'wallet.popFoot': '每分钟自动轮转刷新 · 多账号随机错峰',
  'wallet.noNickname': '（未同步昵称）',
  'wallet.badgePrimary': '主',
  'wallet.badgeCurrent': '当前',
  'wallet.toastRefreshed': '已刷新：{balance}',
  'wallet.toastFailed': '刷新失败，请稍后再试',

  /* 顶栏头像 */
  'avatar.title': '我的 Steam 账号 · {name}',
  /** 头像组件的 `title` prop 缺省值。**不能写进 withDefaults**：那里的默认值只在
   *  defineProps 求值那一刻算一次，会把语言冻结在组件创建时。 */
  'avatar.defaultTitle': '我的 Steam 账号',
  'avatar.status': '{title} · Steam 状态：{status}',
  'avatar.presenceOnline': '在线',
  'avatar.presenceInGame': '游戏中',
  'avatar.presenceOffline': '离线',

  /* 外壳自身的控件（不属于任何路由条目） */
  'shell.tour': '新手教程',
  'shell.backtop': '返回顶部',
  'shell.sidebar.expand': '展开侧边栏',
  'shell.sidebar.collapse': '折叠侧边栏',
  'shell.theme.toLight': '切换到浅色主题',
  'shell.theme.toDark': '切换到深色主题',
  /* 语言钮：文案**显示目标语言**（当前中文 → EN；当前英文 → 中），与主题钮
     「展示切换后状态」同理。两条词条在两种语言下取值相同（各自语言自洽可读），
     英文词典里照抄——这不是漏译，是刻意的语言无关文案。 */
  'shell.lang.toEn': '切换到 English',
  'shell.lang.toZh': 'Switch to 中文',
  /* 语言钮上的短码：显示**目标语言的自称**（语言选择器的通行做法，
     如同 "中文 / English" 并列时各自用自己的语言书写）。故两种语言下同值。 */
  'shell.lang.targetEn': 'EN',
  'shell.lang.targetZh': '中',

  /* 应用更新（启动主动告知 + 侧栏红点） */
  'update.toastAvailable': '发现新版本 v{version}，到「我」页可一键更新',
  'update.navDot': '有新版本可用',

  /* 更新已下载：全局重启提示（下载可能在任意页面完成，提示不能只留在设置页） */
  'shell.updateReady.title': '更新已下载完成',
  'shell.updateReady.body': 'v{version} 已下载并通过校验，重启应用即可完成安装。',
  'shell.updateReady.hint':
    '重启期间界面会短暂关闭，随后自动回到新版；游戏库与账号数据不受影响。',
  'shell.updateReady.restart': '立即重启并更新',
  'shell.updateReady.later': '稍后重启',
} as const

export default shell
