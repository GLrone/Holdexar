/* ════════════════════════════════════════════════════════════════════
   navbar 词条 —— components/business/HlNavbar.vue（库页导航栏：
   搜索 / 排序下拉 / 地区下拉 + 子分类工具栏 / 布局切换 / 高级筛选入口）。

   注意：'navbar.region.allLowest'（下拉按钮上的「无地区筛选」态，前缀另有
   📉）与 'navbar.regionOption.allLowest'（下拉里的选项）是按钮态与选项两个
   位点的 key，当前文案相同但仍分开维护；同理 'navbar.region.locked'（按钮
   态，无锁图标）与 'navbar.regionOption.locked'（下拉选项，整句含 🔒）。
   ════════════════════════════════════════════════════════════════════ */

const navbar = {
  /* 排序下拉：模块级常量表只存 key，显示文本在模板/computed 里现取 */
  'navbar.sort.default': '⭐ 默认排序',
  'navbar.sort.smart': '🧠 智能排序',
  'navbar.sort.rating': '👍 好评优先',
  'navbar.sort.priceDiff': '💰 差价最大',
  'navbar.sort.discount': '🏷️ 折扣力度',
  'navbar.sort.top100': '🔥 近期TOP100热榜',
  'navbar.sort.new2026': '📅 2026年发售',

  /* 地区下拉按钮上的当前地区标签（地区名本身来自 regionsStore，不在此列） */
  'navbar.region.allLowest': '全区最低',
  'navbar.region.locked': '锁国区游戏',

  /* 地区下拉选项（动态地区名来自 regionsStore） */
  'navbar.regionOption.allLowest': '全区最低',
  'navbar.regionOption.cn': '国区',
  'navbar.regionOption.locked': '🔒 锁国区游戏',

  /* 搜索框 placeholder */
  'navbar.search.placeholder': '搜索游戏… (Enter)',

  /* 布局切换按钮的 title */
  'navbar.layout.grid': '网格视图',
  'navbar.layout.list': '列表视图',

  /* 高级筛选入口按钮（不带尾随空格） */
  'navbar.advancedFilter': '⚙️ 高级筛选',

  /* 地区筛选子分类工具栏：过滤模式三连 + 清除 */
  'navbar.filterMode.global': '👑 全区最低',
  'navbar.filterMode.cheaper': '📉 比国区低',
  'navbar.filterMode.highDiff': '💰 潜力大差价',
  'navbar.filter.clear': '✕ 清除',
} as const

export default navbar
