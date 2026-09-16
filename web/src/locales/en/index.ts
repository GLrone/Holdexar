/* ════════════════════════════════════════════════════════════════════
   English 词典汇总。与 `zh-CN/index.ts` 同构，新增模块同样在这里展开一行。

   类型刻意声明为 `Partial<Record<MessageKey, string>>`：**key 受中文词典约束**
   （拼错的 key、中文删了但英文没删的孤儿词条都会在 tsc 阶段报错），而值可缺。

   ⚠️ 「值可缺」是**运行时兜底**，不是工作流：构造上是 Partial（缺译不至于崩），
   但 `scripts/check-i18n.mjs` 把「缺译」判为 error 并挂在 `npm run build` 与
   pre-commit 上——英文界面下回退成中文是用户可见的缺陷，不该进产物。
   所以类型写成 Partial 是为了**不崩**，门禁判 error 是为了**译全**，两者不矛盾。

   另注：tsc 目前跑不起来（tsconfig 的 baseUrl 弃用告警），`vite build` 也不做类型
   检查，故「类型会报错」这条实际由 check-i18n.mjs 兜住，别只依赖编辑器。
   ════════════════════════════════════════════════════════════════════ */

import type { MessageKey } from '../zh-CN'
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
import famPlay from './famPlay'
import famContrib from './famContrib'
import famGrowth from './famGrowth'
import famHeat from './famHeat'
// famLicense (library licenses module) merged into the bills page's Licenses tab; keys live in bills.cdk.*
// ── 期 7 数据层（api/currencies.ts 的币种名、stores/regions.ts 的表外区名）──
import currencies from './currencies'
import regions from './regions'
// ── 共享业务组件（components/business/EpicFreeCards.vue）──
import epicFree from './epicFree'
// ── Game Library page (views/gamelib/Index.vue and its tabs) ──
import gamelib from './gamelib'
// ── Global update modal (components/business/UpdateDialog.vue) ──
import updateDialog from './updateDialog'

const messages: Partial<Record<MessageKey, string>> = {
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
  ...famPlay,
  ...famContrib,
  ...famGrowth,
  ...famHeat,
  ...currencies,
  ...regions,
  ...epicFree,
  ...gamelib,
  ...updateDialog,
}

export default messages
