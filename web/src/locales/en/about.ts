/* English 词典 · about（与 zh-CN/about.ts 同构，key 必须逐一对齐）。

   hero.desc 与各 feature.desc 是**重写**的英文句子，不是逐字对译：这一页是
   项目自述，英文读者要的是读得通的话。 */

import type { MessageKey } from '../zh-CN'

const about: Partial<Record<MessageKey, string>> = {
  /* Hero */
  'about.hero.desc':
    'A self-hosted personal terminal that pulls Steam multi-region pricing, wishlist monitoring, family library analytics, purchase history and proxy routing into one local dashboard. All data stays on this machine, and every outbound request goes through your own proxy chain — never a third-party server.',

  /* Uptime value in the footer */
  'about.uptime.seconds': '{n} sec',
  'about.uptime.minutes': '{n} min',
  'about.uptime.hours': '{h} hr {m} min',
  'about.uptime.days': '{d} d {h} hr',

  /* Section titles */
  'about.section.features': 'Features',
  'about.section.tech': 'Tech stack',
  'about.section.sources': 'Data sources',
  'about.section.privacy': 'Privacy & disclaimer',
  'about.section.credits': 'Credits',

  /* Feature cards */
  'about.feature.multiRegion.title': 'Multi-region price matrix',
  'about.feature.multiRegion.desc':
    '41 regions with live prices, all-time lows and discounts, auto-converted to CNY for side-by-side comparison',
  'about.feature.wishlist.title': 'Watch pool',
  'about.feature.wishlist.desc':
    'Wishlist and owned tracking in one pool, with watched-region picking — sync every 15 min, price crawl every 6 h',
  'about.feature.alerts.title': 'Price alerts',
  'about.feature.alerts.desc':
    'Email alerts on price drops and new all-time lows, with the account owner region shown first',
  'about.feature.rates.title': 'Exchange rates',
  'about.feature.rates.desc':
    'Multi-currency rate charts, plus reseller top-up price lists as a real-cost reference',
  'about.feature.bundles.title': 'Bundles',
  'about.feature.bundles.desc':
    'bundle / sub identity resolution, cross-region bundle prices and an import queue',
  'about.feature.family.title': 'Family library',
  'about.feature.family.desc':
    'Aggregated family-shared library analytics: contribution split, acquisition heatmap, value insights, member activity',
  'about.feature.bills.title': 'Purchase history',
  'about.feature.bills.desc':
    'Multi-currency purchases, all converted to CNY at the rate of the posting date',
  'about.feature.proxies.title': 'Proxy manager',
  'about.feature.proxies.desc':
    'Clash subscription import, real Steam reachability checks per node, cooldown lifecycle management',
  'about.feature.tasks.title': 'Task center',
  'about.feature.tasks.desc':
    'Crawler queue visualisation, batch AppID import, three discovery charts that restock the library',
  'about.feature.toolbox.title': 'Toolbox',
  'about.feature.toolbox.desc': 'Handy utilities such as batch CDK activation',

  /* Tech stack */
  'about.tech.frontend.label': 'Frontend',
  'about.tech.frontend.desc':
    'Vue 3 + TypeScript + Vite + Pinia + ECharts, with a homemade Hl* component system (dark / light)',
  'about.tech.backend.label': 'Backend',
  'about.tech.backend.desc': 'Python FastAPI + SQLAlchemy (async) + SQLite',
  'about.tech.desktop.label': 'Desktop',
  'about.tech.desktop.desc': 'pywebview (WebView2) one-click launch and login hand-off',
  'about.tech.proxy.label': 'Proxy chain',
  'about.tech.proxy.desc': 'mihomo (Clash core) + proxy policy engine (proxy_first)',

  /* Data sources */
  'about.source.price.label': 'Prices',
  'about.source.price.desc':
    'Store-page crawls across regions (41-region rotation, sale pre-check to save quota, 429 back-off)',
  'about.source.account.label': 'Account',
  'about.source.account.desc': 'Official Steam Web API (JWT from cookie, no API key needed)',
  'about.source.network.label': 'Egress',
  'about.source.network.desc':
    'Always through the proxy policy engine (proxy_first); blocked domains are proxied automatically',
  'about.source.flags.label': 'Flags',
  'about.source.flags.desc': 'flagcdn.com;',
  'about.source.rates.label': 'Rates',
  'about.source.rates.desc': 'third-party exchange-rate API',

  /* Privacy & disclaimer */
  'about.privacy.localOnly':
    'Everything — cookies, purchases, price database, family library — lives in a local SQLite file; no third-party server is involved',
  'about.privacy.noBypass':
    'No Steam privacy setting is bypassed; other users profiles can only be read when they are public',
  'about.privacy.disclaimer':
    'Prices and statistics are for reference only; always confirm on the Steam store page before buying',
  'about.privacy.personalUse': 'For personal learning and private use only; no commercial use',

  /* Credits (project names and links stay in the component) */
  'about.credit.steamFamily':
    'Reference for family-library field semantics and metrics (GPLv3 — reference only, no code used)',
  'about.credit.mihomo': 'Clash proxy core (subscription import and node management)',
  'about.credit.flagcdn': 'National and regional flag artwork',
  'about.credit.reactBits':
    'Interaction-style reference for the scroll list and section rail (MIT + Commons Clause; implementations here are original)',
  'about.credit.clashVerge':
    'Reference for subscription auto-naming and filename encoding (GPL-3.0 — behaviour only, no code used)',
  'about.credit.asf': 'Behaviour baseline for the update package swap routine (Apache-2.0)',

  /* Runtime footer */
  'about.env.version': 'Version v{v}',
  'about.env.dataDir': 'Data {path}',
  'about.env.uptime': 'Uptime {d}',
  'about.env.disconnected': 'Backend not connected — runtime info unavailable',
}

export default about
