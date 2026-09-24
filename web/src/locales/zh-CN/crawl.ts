/* crawl 词条 —— 任务中心页（views/crawl/Index.vue）。

   分节锚点 `crawl.section.*` 是**共用**词条：既是 data-section 的属性值
   （HlSectionRail 读到后 t() 显示成悬停气泡与 aria-label），又是区块可见
   标题本身——本页这两处逐字相同，故不另立重复条目。
   ⚠️ `crawl.section.bulkImport` 的字符串被 ProductTour 的 target 选择器引用
   （`[data-section="crawl.section.bulkImport"]`），属**契约**：改名或换词会
   让引导静默选不中，改之前先与 ProductTour 侧同步。

   几处刻意的复用（改一条多处同步）：
   · `crawl.action.*`（清空 / 保存）在批量导入、收藏导入、已购地区三处
     是同一个动作，共用一组；
   · `crawl.import.importing / detected / detectedInvalid` 在两个导入框
     （批量导入 / 收藏列表导入）逐字相同，共用；
   · 「全部」（区服列的空值）不另立——复用 common.all；
   · 「导入中…」的三种写法合并成一条：中文语境下三处完全一致。

   汇总行一律是**参数化整句**，不在组件侧拼片段：进度 meta、导入结果 parts
   （新导入 / 已在库 / 未识别 / 无效 / 首爬已启动）、任务统计行——中英量词与
   语序不同，拆成「标签 + 值」多条再拼拼不回去。parts 之间的 ` · ` 分隔符
   是中性符号，留在组件侧（与 dashboard 的 proxy 副注同理）。

   ⚠️ 收藏列表导入的分节说明含两枚 <code>（FAVORITES_RESPONSE / favorites）：
   本项目的 t() 没有标记插值（没有 <i18n-t> 之类），但**不必**因此切分句子——
   <code> 直接写进词条值、组件侧 v-html 渲染即可（同 bundles.calc.excludeHint）。
   曾按标记边界切三段，理由「两枚标识符在中英里的落点相同」不成立：切分把标识符
   钉死在固定位置，英文只能拼成 "FAVORITES_RESPONSE → favorites"，读成转换关系。 */

const crawl = {
  /* ── 分节锚点（data-section 属性值 + 可见区块标题共用）──
     监控地区的圈定在监控池页（pool.section.regions），本页没有该分节。 */
  'crawl.section.start': '启动爬取',
  'crawl.section.bundleImport': '捆绑包导入',
  'crawl.section.bulkImport': '批量导入', // ← ProductTour 契约，勿改字符串
  'crawl.section.favImport': '收藏列表导入',
  'crawl.section.owned': '已购游戏抓取',
  'crawl.section.jobs': '任务记录',

  /* ── 启动爬取 ── */
  'crawl.start.desc':
    '区服来自「设置」页配置；新游戏首次入库不走打折预检，刷新场景可用预检省请求。',
  'crawl.start.scopeWishlist': '监控池全量',
  'crawl.start.scopeAppids': '指定 AppID',
  'crawl.start.appidsPlaceholder': '例如 620,105600',
  'crawl.start.button': '启动任务',
  'crawl.start.running': '任务进行中…',
  'crawl.start.invalidAppids': '请输入有效的 AppID（逗号分隔）',
  'crawl.start.started': '任务 #{id} 已启动（{count} 个目标）',
  'crawl.stop.button': '停止',
  'crawl.stop.requested': '已请求停止',
  'crawl.stop.none': '没有运行中的任务',
  'crawl.jobs.refresh': '刷新任务记录',

  /* 实时进度（SSE）：一条整句，分隔符在词条内 */
  'crawl.progress.meta':
    '已处理 {done} · 成功 {ok} · 失败 {fail} · 队列 {qsize} · {speed} t/s',

  /* 自动价格链开关 */
  'crawl.autoPrice.label': '自动价格更新',
  'crawl.autoPrice.badgeOn': '自动更新中',
  'crawl.autoPrice.badgeOff': '已转手动',
  'crawl.autoPrice.tip':
    '关闭后定时价格更新停转：6h 主轮（含捆绑包刷新）、失败修复、新增条目的入库首爬都不再自动执行；榜单反哺与手动发起的爬取不受影响（账户同步只同步成员资格）',
  'crawl.autoPrice.on': '自动价格更新已开启：按锚点网格每 6 小时自动爬价',
  'crawl.autoPrice.off': '自动价格更新已关闭：定时爬价停转，价格更新全凭手动',
  'crawl.autoPrice.failed': '设置失败，请重试',

  /* ── 捆绑包导入 ── */
  'crawl.bundle.desc':
    '粘贴 Steam 商店 / SteamDB 捆绑包或 Sub 链接（或裸 ID）；入库后包内游戏自动进爬取队列，已在库的包按刷新处理。',
  'crawl.bundle.import': '导入',
  'crawl.bundle.refreshed': '已刷新：{name}',
  'crawl.bundle.imported': '已导入：{name}',

  /* ── 批量导入（加入监控池 + 首爬入库）── */
  'crawl.bulk.desc':
    '粘贴 Steam 商店 / SteamDB 游戏链接或裸 AppID（空格、逗号、换行分隔均可，自动去重）；导入即加入监控池并首爬入库——池内游戏全部参与价格爬取，要升到第一优先级请在 Steam 愿望单保留它或点星标关注。',
  'crawl.bulk.import': '导入监控',
  'crawl.bulk.moreHidden': '其余 {n} 条明细从略 —— 到「游戏商店」页查看全部',

  /* ── 收藏列表导入（FAVORITES 通道）──
     两枚 <code> 写在词条值里、组件侧 v-html 渲染（同 bundles.calc.excludeHint）。
     曾按标记边界切三段（descPrefix / descMid / descSuffix），理由是「两枚代码
     标识符在中英里的落点相同」——那个理由不成立：切分把标识符钉死在固定位置，
     英文就没有余地写「FAVORITES_RESPONSE 里的 favorites」，只能拼成
     "FAVORITES_RESPONSE → favorites"，读成了转换关系（原意是嵌套关系）。
     词条是应用自有静态文案（非用户输入），v-html 无注入面；本处无 code 样式，
     故 <code> 走浏览器默认，与迁移前逐字相同。 */
  'crawl.fav.desc':
    '浏览器脚本控制台导出的收藏列表（<code>FAVORITES_RESPONSE</code> 的 <code>favorites</code> JSON，数字数组或对象数组）整段粘贴即可；导入即加入监控池，未入库的游戏自动爬取入库作监控数据。',
  'crawl.fav.import': '导入收藏',
  'crawl.fav.placeholder':
    '[2561580,1173800,1173820,3837340]\n或 [{"appid":620,"name":"Portal 2"},…]',
  /* 教程窗：这条通道的数据来自浏览器脚本，脚本原文与控制台代码一并给出，
     用户不必再去别处翻（脚本随前端产物分发，见 crawl/Index.vue 的说明）。
     文件名常量在组件里（词典不收文件名——英文值里留中文会被词典门禁判为残留） */
  'crawl.fav.tutorial': '导入教程',
  'crawl.fav.tutTitle': '收藏列表导入教程',
  'crawl.fav.tut.step1': '安装 Tampermonkey（油猴）扩展，把下方脚本添加进去（或下载文件后拖进扩展面板）。',
  'crawl.fav.tut.step2': '打开脚本生效的页面（本地 HTML 报告 / Steam 收藏页），脚本会把你的收藏存到本地。',
  'crawl.fav.tut.step3': '按 F12 打开控制台，粘贴并执行下方命令——收藏列表会以 JSON 复制到剪贴板。',
  'crawl.fav.tut.step4': '回到本页粘贴进输入框并点「导入收藏」：导入即加入监控池，未入库的游戏自动首爬。',
  'crawl.fav.tut.console': '控制台命令（在脚本生效的页面执行）',
  'crawl.fav.tut.script': '油猴脚本：SteamHL 助手',
  'crawl.fav.tut.copy': '复制',
  'crawl.fav.tut.copied': '已复制',
  'crawl.fav.tut.download': '下载脚本',
  'crawl.fav.tut.scriptEmpty': '脚本未取到——关窗重试，或用上面的下载链接',

  /* ── 两个导入框共用 ── */
  'crawl.import.importing': '导入中…',
  'crawl.import.detected': '识别到 {n} 个 AppID',
  'crawl.import.detectedInvalid': '{n} 条未识别',
  'crawl.import.added': '新导入 {n}',
  'crawl.import.alreadyTracked': '已在库 {n}',
  'crawl.import.poolAdded': '加入监控池 {n}',
  'crawl.import.unrecognized': '未识别 {n}',
  'crawl.import.invalid': '无效 {n}',
  'crawl.import.firstCrawlStarted': '首爬已启动',
  /* 无法解析时的整句提示：末段「等」随是否截断二选一，故拆成两条完整句
     （而不是拼接「列表 + 等」两个片段），中英各自成句。 */
  'crawl.import.noValidAppid': '没有识别到 AppID（无法解析：{list}）',
  'crawl.import.noValidAppidMore': '没有识别到 AppID（无法解析：{list} 等）',
  'crawl.import.noValidItems': '没有识别到有效条目（无法解析：{list}）',
  'crawl.import.noValidItemsMore': '没有识别到有效条目（无法解析：{list} 等）',

  /* ── 通用动作（清空 / 保存；全选已随监控地区分节迁走，走 common.selectAll）── */
  'crawl.action.clear': '清空',
  'crawl.action.save': '保存',

  /* ── 区服计数（已购游戏抓取地区分节的「跟随」计数展示）──
     圈定监控地区的完整分节在监控池页（pool.regions.*），本页只留
     这一条计数句式给已购分节的「自定义」态角标用。 */
  'crawl.regions.selected': '已选 {n} / {total} 区',

  /* ── 已购游戏抓取地区（独立于监控地区的子集配置；圈定入口在监控池页）── */
  'crawl.owned.desc':
    '「账户设置」按账户开关已购库同步（库太大的账户可关，只盯愿望单）；已购游戏随愿望单同步自动进监控池，地区跟随监控地区（默认，监控池页圈定）或圈定子集省配额。',
  'crawl.owned.acctSet': '账户设置',
  'crawl.owned.acctSetTip': '按账户开关已购库同步',
  'crawl.owned.follow': '跟随监控地区',
  'crawl.owned.hintFollow': '已购游戏与愿望单共用同一套区服抓取（监控池页可改）',
  'crawl.owned.hintCustom': '已购游戏仅抓取下方勾选的区服（至少一个）',
  'crawl.owned.savedFollow': '已保存：已购游戏跟随监控地区抓取',
  'crawl.owned.savedCustom': '已保存：已购游戏将抓取 {n} 个区的价格',
  'crawl.owned.emptyWarn': '已购抓取地区不能为空：至少勾选一个区服，或改回「跟随监控地区」',

  /* ── 已购同步账户弹窗 ── */
  'crawl.acct.title': '已购同步账户',
  'crawl.acct.hint':
    '开 = 该账户已购库并入监控池；库太大的账户可关，只同步愿望单。开关下次同步生效。',
  'crawl.acct.meta': '好友码 {code} · 已购 {n} 款',
  'crawl.acct.empty': '还没有绑定账户 —— 到「监控池」页绑定或同步家庭组。',
  'crawl.acct.ownedOn': '已开启 {name} 的已购同步，下次同步生效',
  'crawl.acct.ownedOff': '已关闭 {name} 的已购同步，下次同步生效',

  /* ── 任务记录表 ── */
  'crawl.jobs.kind': '类型',
  'crawl.jobs.status': '状态',
  'crawl.jobs.regions': '区服',
  'crawl.jobs.stats': '统计',
  'crawl.jobs.startedAt': '开始时间',
  'crawl.jobs.statsText': '{ok} 成功 / {fail} 失败 / {skip} 预检跳过 · {secs}s',

  /* 任务状态（表格状态列） */
  'crawl.status.done': '已完成',
  'crawl.status.running': '进行中',
  'crawl.status.failed': '失败',
  'crawl.status.stopped': '已停止',

  /* 任务类型（后端 kind → 标签；后端新 kind 会原样回落到 kind 值） */
  'crawl.kind.manual': '手动',
  'crawl.kind.scheduled': '定时',
  'crawl.kind.wishlistSync': '愿望单同步',
  'crawl.kind.ownedSync': '已购同步',
  'crawl.kind.missing': '欠账补抓',
  'crawl.kind.repair': '失败修复',
  'crawl.kind.backfill': '孤儿回补',
  'crawl.kind.import': '批量导入',
  'crawl.kind.favImport': '收藏导入',
  'crawl.kind.poolAdd': '添加监控',
  'crawl.kind.familyWishlist': '家庭愿望单同步',
  'crawl.kind.removedRetry': '下架重试',
  'crawl.kind.comingsoonRetry': '即将推出重探',
  'crawl.kind.freePromoRetry': '免费促销重试',
  'crawl.kind.top100Backfill': '热销榜回补',
  'crawl.kind.popularnewBackfill': '热门新品回补',
  'crawl.kind.specialsBackfill': '特惠榜回补',
  'crawl.kind.comingsoonBackfill': '即将推出回补',
} as const

export default crawl
