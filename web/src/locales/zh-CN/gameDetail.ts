/* gameDetail 词条 —— 游戏详情页（views/game-detail/Index.vue）。

   几条刻意的取舍，改之前先读：

   · **分节锚点**（`gameDetail.section.*`）的 key 就是 `data-section` 的属性值本身，
     HlSectionRail 读到后 t() 显示（锚点与语言无关，ProductTour 的选择器不会随切
     语言断掉）。其中 priceDetail / trend 两条**同时是区块可见标题**——标题与锚点
     是同一个字符串，共用一条（dashboard.section.* 同例，一处定义两处消费）。

   · 本页与 PriceTrendDrawer / PriceTrendChart 共用同一批术语，下列词条是**同义镜像**，
     两份英文必须逐字一致（check-i18n.mjs 判不出跨模块的同义分歧，靠人记）：
       gameDetail.range.*        ←→ trendDrawer.range.*
       gameDetail.event.*        ←→ trendDrawer.event.*
       gameDetail.trend.lowest   ←→ trendDrawer.legend.lowest / trendChart.legend.lowest
       gameDetail.trend.discount ←→ trendDrawer.price.discount
       gameDetail.trend.noDiscount ←→ trendDrawer.price.noDiscount
       gameDetail.trend.steamNow ←→ trendChart.legend.steam
       gameDetail.trend.empty     ←→ trendChart.empty
       gameDetail.trend.emptyRegion ←→ trendChart.empty（前半句换成本页的「该地区」）
     按模块拆 key 是既定约定（各模块可独立改动），代价就是上面这张表要人记。

   · `gameDetail.rating.positive` / `rating.reviews` / `price.save` / `price.col.*` /
     `bundle.*` / `badge.hb` / `hl.*` / `pp.cut` / `region.cn` / `link.steamStore` /
     `price.locked` / `price.lowestTag` / `removed.tag` / `removed.probing` 也与
     gameCard 模块的同义词条对齐（措辞沿用已有译法，不另起一套）。

   · 后端数据值（`detail.type` / `detail.chineseSupport` / 地区名与货币码）本身仍是
     中文枚举，属数据层词汇；本模块只迁「标签」，值原样渲染。 */

const gameDetail = {
  /* ── 分节锚点（data-section 属性值）+ 同名区块标题 ── */
  'gameDetail.section.header': '游戏详情',
  'gameDetail.section.priceDetail': '全区价格明细',
  'gameDetail.section.trend': '历史价格走势',
  'gameDetail.section.bundles': '关联捆绑包',

  /* ── 渐变头部 ── */
  'gameDetail.header.back': '返回找游戏',
  'gameDetail.header.developers': '开发：{names}',
  'gameDetail.header.publishers': '发行：{names}',
  'gameDetail.region.cn': '国区',
  'gameDetail.summary.lowestRegion': '{region}最低',
  'gameDetail.summary.savings': '可省',
  'gameDetail.hl.newLow': '新史低',
  'gameDetail.hl.sameLow': '平史低',
  'gameDetail.discount.endsAt': '折扣 {date} 截止',
  'gameDetail.pp.cut': '永降',
  'gameDetail.pp.raise': '原价上调',
  'gameDetail.link.steamStore': 'Steam 商店',

  /* ── 下架提示条（移除监控）── */
  'gameDetail.removed.tag': '已下架',
  'gameDetail.removed.judged': '{date} 判定移除',
  'gameDetail.removed.retry': '重新探测',
  'gameDetail.removed.probing': '探测中…',
  'gameDetail.removed.requeued': '已发起重爬，稍后刷新查看',
  'gameDetail.removed.cleared': '已清除下架标记，下一轮监控自动带上',

  /* ── 信息侧栏 ── */
  'gameDetail.info.basic': '基本信息',
  'gameDetail.info.type': '类型：{type}',
  'gameDetail.info.releaseDate': '发售日：{date}',
  'gameDetail.info.chinese': '中文：{value}',
  'gameDetail.info.series': '系列：{id}',
  'gameDetail.info.developers': '开发商',
  'gameDetail.info.publishers': '发行商',
  'gameDetail.info.genres': '类型标签',
  'gameDetail.info.reviews': '评测',
  'gameDetail.rating.positive': '好评 {rate}%',
  'gameDetail.rating.reviews': '{n} 篇评测',
  'gameDetail.info.features': '特性',

  /* 特性徽章（XGP 那条只剩品牌词 + 后端档位，不建词条） */
  'gameDetail.badge.familySharing': '家庭共享',
  'gameDetail.badge.tradingCards': '集换式卡牌',
  'gameDetail.badge.adult': '成人内容',
  'gameDetail.badge.visualNovel': '视觉小说',
  'gameDetail.badge.epic': 'EPIC 送过',
  'gameDetail.badge.epicDate': 'EPIC 送过 ({date})',
  'gameDetail.badge.hb': 'HB 慈善包',
  'gameDetail.badge.bundled': '进过{n}包',

  /* ── 全区价格明细 ── */
  'gameDetail.price.col.region': '区域',
  'gameDetail.price.col.native': '本币价格',
  'gameDetail.price.col.cny': 'CNY 换算',
  'gameDetail.price.col.discount': '折扣',
  'gameDetail.price.col.save': '节省',
  'gameDetail.price.top3': '最低三区',
  'gameDetail.price.medalTip': '全区最低价排名（非国区）',
  'gameDetail.price.lowestTag': '最低',
  'gameDetail.price.locked': '锁区',
  'gameDetail.price.free': '免费',
  'gameDetail.promo.active': '限时免费 · {date} 截止',
  'gameDetail.price.save': '省¥{amount}',
  'gameDetail.price.empty': '暂无有效价格数据',

  /* ── 历史价格走势 ── */
  'gameDetail.range.90d': '90天',
  'gameDetail.range.1y': '1年',
  'gameDetail.range.3y': '3年',
  'gameDetail.range.all': '全部',
  /* 地区下拉选项：地区名与货币码都是数据，但全角括号是中文排版——英文侧换半角 */
  'gameDetail.region.option': '{name}（{currency}）',
  'gameDetail.trend.steamNow': 'Steam 现价',
  'gameDetail.trend.discount': '▼ -{pct}% · 折扣中',
  'gameDetail.trend.noDiscount': '未打折',
  'gameDetail.trend.emptyRegion': '该地区暂无历史数据 —— 随着定时爬取积累，走势图会逐渐成形。',
  'gameDetail.trend.lowest': '历史最低',
  'gameDetail.trend.debutPrice': '首发原价',
  'gameDetail.trend.lowNodeCount': '{n} 个',
  'gameDetail.trend.lowNodes': '史低节点',
  'gameDetail.trend.empty': '暂无历史数据 —— 随着定时爬取积累，走势图会逐渐成形。',
  /* 史低节点时间线标记 */
  'gameDetail.event.first': '首发',
  'gameDetail.event.lowest': '史低',
  'gameDetail.event.drop': '降至',

  /* ── 关联捆绑包 ── */
  'gameDetail.bundles.title': '关联捆绑包（{n}）',
  'gameDetail.bundle.completable': '可补齐',
  'gameDetail.bundle.wholeOnly': '必须整包',
  'gameDetail.bundle.unknown': '状态未知',
} as const

export default gameDetail
