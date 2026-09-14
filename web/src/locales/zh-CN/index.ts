/* ════════════════════════════════════════════════════════════════════
   中文词典汇总。**zh-CN 是回退源，也是 `MessageKey` 的唯一推导来源**——
   新增词条先落这里，`en` 补译；缺译自动回退中文（见 locales/index.ts）。

   为什么按模块拆文件：全量双语后这里会有上千条 key，单文件既不可维护也
   必然在多人/多会话编辑时冲突。约定是**模块文件与视图/组件同构**：
   `views/proxies/Index.vue` 的文案落 `zh-CN/proxies.ts`，迁移到哪个文件
   就建哪个模块——这样「还有哪些模块没迁」一眼可见。

   新增一个模块只需两步：建 `<module>.ts`（`as const` 默认导出），在这里
   展开一行。`as const` 不能省：`MessageKey` 靠它才推得出字面量联合类型，
   少了它 t() 会退化成接受任意 string，词典与调用点之间的类型校验就没了。
   ════════════════════════════════════════════════════════════════════ */

import common from './common'
import shell from './shell'
// ── 共享业务组件（components/business/*，五个组件各一模块）──
import filterPanel from './filterPanel'
import gameCard from './gameCard'
import navbar from './navbar'
import trendChart from './trendChart'
import trendDrawer from './trendDrawer'
// ── 期 4 高频独立视图（views/<name>/Index.vue，一视图一模块）──
import about from './about'
import bundles from './bundles'
import dashboard from './dashboard'
import library from './library'
import logs from './logs'
import rates from './rates'
import toolbox from './toolbox'
// ── 期 5 独立视图与组件（views/<name>/Index.vue、components/ProductTour.vue）──
import proxies from './proxies'
import settings from './settings'
import crawl from './crawl'
import gameDetail from './gameDetail'
import alerts from './alerts'
import bills from './bills'
import productTour from './productTour'
import pool from './pool'
// ── 期 6 家庭组及其页签（views/family/Index.vue、views/family/tabs/*.vue）──
import family from './family'
import famWish from './famWish'
import famLib from './famLib'
import famBuy from './famBuy'
import famValue from './famValue'
import famPlay from './famPlay'
import famContrib from './famContrib'
import famInsight from './famInsight'
import famGrowth from './famGrowth'
import famHeat from './famHeat'
// famLicense（入库许可证模块）已并入 bills 页许可证页签，词条归 bills.cdk.*
// ── 期 7 数据层（api/currencies.ts 的币种名、stores/regions.ts 的表外区名）──
//    它们不属于任何视图，故不与视图同构，而是与**数据来源模块**同构。
import currencies from './currencies'
import regions from './regions'
// ── 共享业务组件（components/business/EpicFreeCards.vue）──
import epicFree from './epicFree'
// ── 游戏库页（views/gamelib/Index.vue 及其页签）──
import gamelib from './gamelib'

/** 全量中文词典 */
export const messages = {
  ...common,
  ...shell,
  ...gameCard,
  ...filterPanel,
  ...trendDrawer,
  ...navbar,
  ...trendChart,
  ...toolbox,
  ...bundles,
  ...about,
  ...dashboard,
  ...rates,
  ...library,
  ...logs,
  ...proxies,
  ...settings,
  ...crawl,
  ...gameDetail,
  ...alerts,
  ...bills,
  ...productTour,
  ...pool,
  ...family,
  ...famWish,
  ...famLib,
  ...famBuy,
  ...famValue,
  ...famPlay,
  ...famContrib,
  ...famInsight,
  ...famGrowth,
  ...famHeat,
  ...currencies,
  ...regions,
  ...epicFree,
  ...gamelib,
} as const

export type MessageKey = keyof typeof messages

export default messages
