/* gameCard 词条 —— 游戏卡（components/business/HlGameCard.vue）。

   几处刻意的复用，改一条两边同步：
   · 「全区价格」同时是卡片按钮与 GPW 弹窗小标题（共用 regionPrice.title）。
   · 「省¥{amount}」在卡片差价徽章与关联捆绑包条目里是同一件事（共用 price.save）。
   · 「待更新」「锁区」在列表 tab 与柱状图 tab 各出现一次（各一条）。
   · 归属三态徽章的**显示字**与**弹窗标题**分列两条：同一个 owned 在两处
     语义不同（「已拥有」/「归属账号」），英文也不是同一个词。

   带 {占位符} 的条目**不要**在组件侧用 + 拼中文片段：中英语序不同，
   拼不出版行（如 gift.sendReceiverPriced 的两处 {region}）。

   注意 STATUS_META 那类模块级常量只能存 **key**（见组件内注释）——这里
   一行是一条静态文案，没有「按量级分档」的取值，评测数走 reviewCount 一条
   （量级是数字格式，由组件侧的 Intl compact 现取语言，不进词典）。 */

const gameCard = {
  /* ── 折扣 / 史低标签（hlFlag：1 新史低 2 平史低 其余非史低）── */
  'gameCard.hl.newLow': '新史低',
  'gameCard.hl.sameLow': '平史低',
  'gameCard.hl.notLow': '非史低',

  /* ── 折扣徽章悬停气泡（截止日期随促销元数据下发，无数据不弹）── */
  'gameCard.discount.endsAt': '折扣 {date} 结束',

  /* ── EPIC / HB 徽章（品牌词 EPIC 无日期时不建条，留在组件侧）── */
  'gameCard.epic.given': 'EPIC {date} 送过',
  'gameCard.hb.bundle': 'HB慈善包',

  /* ── 下架角标与 tooltip（tooltip 的日期是移除判定日）── */
  'gameCard.removed.tag': '已下架',
  'gameCard.removed.tip': '已从 Steam 商店移除（{date} 判定）',

  /* ── 归属状态徽章 / 悬停弹窗标题 ── */
  'gameCard.status.owned': '已拥有',
  'gameCard.status.ownedTitle': '归属账号',
  'gameCard.status.family': '家庭共享',
  'gameCard.status.familyTitle': '家庭共享来源',
  'gameCard.status.wishlist': '愿望单',
  'gameCard.status.wishlistTitle': '愿望单所属',
  'gameCard.status.wishlistMore': '愿望单 +{n}',

  /* ── 归属弹窗里的账号名兜底（后端未给显示名）── */
  'gameCard.owner.unknown': '未知',

  /* ── 关注星标（状态存后端追踪池；失败时优先弹后端 detail，此条兜底）── */
  'gameCard.follow.tip': '关注 / 取消关注',
  'gameCard.follow.fail': '关注操作失败',

  /* ── 评测：好评率（null 落 rating.none）与评测数（量级由组件侧定）── */
  'gameCard.rating.positive': '好评{rate}',
  'gameCard.rating.none': '好评暂无',
  'gameCard.reviewCount': '{n} 评测',

  /* ── 属性 tags（悬停 tooltip）── */
  'gameCard.tag.familySharing': '支持家庭共享',
  'gameCard.tag.tradingCards': '包含 Steam 集换式卡牌',
  'gameCard.tag.ppCut': '永降',
  'gameCard.tag.ppCutTip': '国区原价曾永久下调',

  /* ── Steam 商店外链（品牌词 STEAM 与「商店」是同一句标签，整句成条）── */
  'gameCard.steamLink': 'STEAM商店',

  /* ── 价格区 ── */
  'gameCard.price.cn': '国区',
  'gameCard.price.free': '免费',
  'gameCard.price.cnLowest': '中国 最低',
  'gameCard.price.diff': '差价',
  'gameCard.price.save': '省¥{amount}',
  'gameCard.price.noDiff': '无差价',

  /* ── 卡片操作行（regionPrice.title 与 GPW 弹窗小标题同条）── */
  'gameCard.regionPrice.title': '全区价格',
  'gameCard.trend.tip': '价格历史走势',
  'gameCard.trend.label': '走势',

  /* ── GPW 弹窗 tab（图标按钮的 title）── */
  'gameCard.tabs.list': '列表',
  'gameCard.tabs.chart': '柱状图',

  /* ── 地区格状态（列表 tab 与柱状图 tab 共用）── */
  'gameCard.region.unavailableTip': '该地区价格未成功更新（爬虫抓取失败），等待下轮补抓',
  'gameCard.region.clickGiftTip': '点击查看赠礼分析',
  'gameCard.region.pending': '待更新',
  'gameCard.region.locked': '锁区',

  /* ── 同系列区块（服务端名称聚类，未识别到不渲染）── */
  'gameCard.series.count': '{n} 款',
  'gameCard.series.self': '本作',
  'gameCard.series.openTip': '查看详情',

  /* ── 关联捆绑包区块 ── */
  'gameCard.bundles.title': '关联捆绑包 ({n})',
  'gameCard.bundles.completable': '可补齐',
  'gameCard.bundles.cnPrice': '国区 ¥{amount}',

  /* ── 第三方平台（CDK 查价）── */
  'gameCard.cdk.title': '第三方平台',
  'gameCard.cdk.loading': '查询中…',
  'gameCard.cdk.notListed': '未收录',

  /* ── 进包史（Barter.vg 计数）── */
  'gameCard.bundled.tag': '进过{n}包',
  'gameCard.bundled.tip': '此游戏曾出现在 {n} 个第三方捆绑包中（数据来源：Barter.vg）',

  /* ── 赠礼分析弹窗 ── */
  'gameCard.gift.title': '赠礼地区分析',
  'gameCard.gift.member': '成员{id}',
  'gameCard.gift.friends': '好友互动 ({name} · {region} 基准价 ¥{price})',
  'gameCard.gift.friendsNoPrice': '好友互动 ({name} · {region} 基准价 —（该区无价格）)',
  'gameCard.gift.noPrimaryPrice': '主账号区（{region}）暂无该游戏价格数据，好友实付无法计算',
  'gameCard.gift.sendLabel': '送出：',
  'gameCard.gift.receiveLabel': '收自：',
  'gameCard.gift.sendReceiverPriced':
    '{from} 送 {to}：{region}区价超基准 ×1.15，实付 ¥{amount}（{region}区价）',
  'gameCard.gift.sendBase': '{from} 送 {to}：实付 ¥{amount}（基准区价）',
  'gameCard.gift.receiveReceiverPriced':
    '{from} 送 {to}：基准区价超 {region} 区价 ×1.15，TA 实付 ¥{amount}（基准区价）',
  'gameCard.gift.receiveBase': '{from} 送 {to}：TA 实付 ¥{amount}（{region}区价）',
  'gameCard.gift.globalSend': '全域送礼能力 (作为发起方)',
  'gameCard.gift.canSendLabel': '可送：',
  'gameCard.gift.cannotSendLabel': '不可：',
  'gameCard.gift.sendGlobalPriced':
    '{region}区价超出本区 ×1.15，实付 ¥{amount}（按收礼方区价）',
  'gameCard.gift.sendGlobalBase': '{region}：实付 ¥{amount}（按本区区价）',
  'gameCard.gift.globalReceive': '全域收礼能力 (作为接收方)',
  'gameCard.gift.canReceiveLabel': '可收：',
  'gameCard.gift.receiveGlobalPriced': '{region} 送礼方须付 ¥{amount}（本区区价超出 ×1.15）',
  'gameCard.gift.receiveGlobalBase': '{region} 送礼方实付 ¥{amount}（{region}区价）',
  'gameCard.gift.rule':
    '规则：收礼方 ≤ 送礼方区价 ×1.15 按送礼方区价付款；超出按收礼方区价付款（↗）；锁区不可送',
} as const

export default gameCard
