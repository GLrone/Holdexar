/* bundles 词条 —— 捆绑包浏览视图（views/bundles/Index.vue）。

   几处刻意的复用，改一条两处同步：
   · 「✅ 支持补齐」「❌ 不支持补齐」「❓ 未知」在**两个位置**是同一件事——
     卡片标签行（bundle-mps-tag）与抽屉「补齐状态」栏（statusBar computed）。
     共用 mps.* 三条，不各写一份。
   · 只有「已拥有全部内容 / 家庭组已拥有全套」是状态栏独有（见 status.owned /
     status.family）。

   带 {占位符} 的词条**不要**在组件侧用 + 或模版字符串拼中文片段：中英语序
   不同（如「共 {n} 个捆绑包」对 '{n} bundles'），拼不出版行。
   · calc.excludeHint **整句进词条，行内强调用 <em class="calc-hint-em"> 包在值里**，
     组件侧 v-html 渲染（同 HlBanner 的 item.html 先例）。曾按强调边界切成
     Prefix/Word/Suffix 三连，理由是「两语言里被强调的词位置一致」——那个
     理由不成立：切分等于**逼译文把被强调的词永远放在句尾**，英文那样写出来是
     "💡 Click any games below that you do not want."，缺了 to buy，是残句。
     词条是应用自有静态文案（非用户输入），v-html 无注入面。

   gameTag.* 值**带前导空格**（拼在游戏名之后：`名字 [已拥有]`），不是笔误。 */

const bundles = {
  /* 抽屉「补齐状态」栏（statusBar computed；completable/unknown 复用 mps.*） */
  'bundles.status.owned': '✅ 已拥有全部内容',
  'bundles.status.family': '✅ 家庭组已拥有全套',

  /* 整包购买语义标签（卡片标签行 + 状态栏共用） */
  'bundles.mps.completable': '✅ 支持补齐',
  'bundles.mps.setOnly': '❌ 不支持补齐',
  'bundles.mps.unknown': '❓ 未知',
  'bundles.mps.completableTip': '可只买缺少的部分并享受整包基础折扣',
  'bundles.mps.setOnlyTip': '必须整包购买',

  /* 游戏名后缀徽标（gameTitle()，含前导空格） */
  'bundles.gameTag.owned': ' [已拥有]',
  'bundles.gameTag.family': ' [家庭组: {owners}]',
  'bundles.gameTag.wishlist': ' [愿望单: {owners}]',

  /* 列表头 */
  'bundles.head.total': '共 {n} 个捆绑包',
  'bundles.head.completable': '· 可补齐 {n} 个',
  'bundles.empty.noData': '暂无捆绑包数据',
  'bundles.list.loadMore': '加载更多（剩余 {n} 个）',

  /* 归属徽章（封面上的 status-badge；`我` 进 data-owners 属性） */
  'bundles.badge.me': '我',
  'bundles.badge.owned': '已拥有',
  'bundles.badge.family': '家庭组',

  /* 卡片价格区 */
  'bundles.tag.baseDiscount': '基础折扣 {pct}%',
  'bundles.link.store': 'STEAM商店',
  'bundles.price.none': '暂无',
  'bundles.price.lowest': '最低',
  'bundles.price.diff': '差价',
  'bundles.price.save': '省{amt}',
  'bundles.price.noDiff': '无差价',
  'bundles.action.allRegionPrices': '全区价格',

  /* 详情抽屉 */
  'bundles.action.steamStore': 'Steam 商店',
  'bundles.action.calc': '✨ 计算购买捆绑包价格',
  'bundles.drawer.statusLabel': '补齐状态:',
  'bundles.drawer.lockHint': 'ℹ️ 浅黄色背景表示该地区部分游戏锁区',
  'bundles.drawer.detailError': '详情加载失败：{err}',
  'bundles.drawer.giftTooltip': '点击查看「{region}」赠礼地区分析',
  'bundles.drawer.lockedTip': '{n} 款游戏在该区锁区',
  'bundles.drawer.lockedBadge': '{n}锁',
  'bundles.drawer.locked': '锁区',

  /* 赠礼地区分析 */
  'bundles.gift.head': '🎁 以「{region}」为赠礼目标',
  'bundles.gift.collapse': '收起',
  'bundles.gift.canGive': '✅ 可赠出（未锁区均可送）',
  'bundles.gift.cannotGive': '⛔ 不可赠出',
  'bundles.gift.canReceive': '📥 可接收来自',

  /* 包内游戏 */
  'bundles.games.title': '🎮 捆绑包包含游戏',
  'bundles.games.empty': '无游戏数据',
  'bundles.games.viewAll': '查看全部 {n} 款游戏',

  /* 地区 AppID 差异（AGR） */
  'bundles.agr.title': '地区 AppID 差异：',
  'bundles.agr.count': '{n} 款游戏',

  /* 「游戏全览 / AGR 差异」弹窗标题（模板参数在 computed 里现取） */
  'bundles.dialog.allGamesTitle': '🎮 捆绑包包含 {n} 款游戏',
  'bundles.dialog.agrTitle': '🎮 差异地区（{regions}）包含 {n} 款游戏',

  /* 补齐计算器 */
  'bundles.calc.title': '🧮 捆绑包价格计算器',
  'bundles.calc.region': '选择地区:',
  'bundles.calc.foreignTotal': '预估外币总价:',
  'bundles.calc.cnyTotal': '换算CNY价格:',
  'bundles.calc.run': '计算',
  'bundles.calc.excludeHint': '💡 请在下方列表中选出<em class="calc-hint-em">不购买</em>的游戏',
  'bundles.calc.autoPreselect': '(已自动预选您的库中拥有及无法获取数据的无效商品)',
  'bundles.calc.noValidItems': '无有效商品',
  'bundles.calc.noPrice': '暂无价格',
  'bundles.calc.noGames': '该地区暂无游戏明细',

  /* 锁区蒙层 */
  'bundles.mask.regionLocked': '🔒锁区',
} as const

export default bundles
