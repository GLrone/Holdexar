/* English 词典汇总。与 zh-CN/index.ts 同构，新增模块同样在这里展开一行。
   类型声明为 Partial<Record<MessageKey, string>>：key 受中文词典约束（拼错的 key 与孤儿词条在 tsc 报错），值可缺。
   注意：缺译由 check-i18n.mjs 判 error（挂 npm run build 与 pre-commit）——运行时回退中文是用户可见缺陷，不是可用状态；
   vite build 不做类型检查，词典问题以 check-i18n 为准。 */

import type { MessageKey } from '../zh-CN'
import common from './common'
import shell from './shell'
// ── 共享业务组件（components/business/*，五个组件各一模块）──
import filterPanel from './filterPanel'
import gameCard from './gameCard'
import navbar from './navbar'
import trendChart from './trendChart'
import trendDrawer from './trendDrawer'
// ── 高频独立视图（views/<name>/Index.vue，一视图一模块）──
import about from './about'
import bundles from './bundles'
import dashboard from './dashboard'
import library from './library'
import logs from './logs'
import rates from './rates'
import toolbox from './toolbox'
// ── 独立视图与组件（views/<name>/Index.vue、components/ProductTour.vue）──
import proxies from './proxies'
import settings from './settings'
import crawl from './crawl'
import gameDetail from './gameDetail'
import alerts from './alerts'
import bills from './bills'
import productTour from './productTour'
import pool from './pool'
// ── 家庭组及其页签（views/family/Index.vue、views/family/tabs/*.vue）──
import family from './family'
import famWish from './famWish'
import famLib from './famLib'
import famBuy from './famBuy'
import famPlay from './famPlay'
import famContrib from './famContrib'
import famGrowth from './famGrowth'
import famHeat from './famHeat'
// famLicense (library licenses module) merged into the bills page's Licenses tab; keys live in bills.cdk.*
// ── 数据层词汇（api/currencies.ts 的币种名、stores/regions.ts 的表外区名）──
import currencies from './currencies'
import regions from './regions'
// ── 共享业务组件（components/business/EpicFreeCards.vue）──
import epicFree from './epicFree'
// ── 共享业务组件（components/business/SteamFreeCards.vue）──
import steamFree from './steamFree'
// ── 共享业务组件（components/business/HbChoiceCards.vue）──
import hbChoice from './hbChoice'
// ── Game Library page (views/gamelib/Index.vue and its tabs) ──
import gamelib from './gamelib'
// ── Global update modal (components/business/UpdateDialog.vue) ──
import updateDialog from './updateDialog'
// ── Achievement hall (views/achievements/Index.vue and DetailDrawer.vue) ──
import achievements from './achievements'
// ── Game career (career/ children and interleaved sections of views/achievements/) ──
import achievementsCareer from './achievementsCareer'
// ── Price event surfaces (game detail "recent price changes" + dashboard digest) ──
import priceEvent from './priceEvent'
// ── Auto Fetch page (views/fetch/Index.vue, per-source switches) ──
import fetchPage from './fetch'
// ── Dynamic-island message surface (components/ui/HlIsland.vue) ──
import island from './island'
// ── Steam event calendar (views/events/Index.vue + SteamEventCountdown card) ──
import steamEvents from './steamEvents'
// ── Pilot assistant (components/business/HlPilotDrawer.vue + settings Pilot card) ──
import pilot from './pilot'

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
  ...steamFree,
  ...hbChoice,
  ...gamelib,
  ...updateDialog,
  ...achievements,
  ...achievementsCareer,
  ...priceEvent,
  ...fetchPage,
  ...island,
  ...steamEvents,
  ...pilot,
}

export default messages
