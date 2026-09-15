/* English 词典 · productTour（与 zh-CN/productTour.ts 同构，key 必须逐一对齐）。
   对应源文件：components/ProductTour.vue。

   这一份是**重写**而非直译：中文原文是口语化的引导语气，逐字对译会变成说明书，
   而且英文比中文长 1.5–2 倍，直译会撑破 440px 宽的气泡。英文按英语产品的引导
   文案写：第二人称、动词开头、一句一件事，长度与中文相当（几个长段落是内容
   本身有三个并列选项，不是没精简）。

   点名界面标签时的取词出处（与各页词典逐字一致，改一处要两处同步）：
   · Store / Proxies / Me / Tasks / Alerts / Watch pool / Rates / Toolbox → shell.ts 的 nav.*
   · Save subscription → proxies.sub.save · Install kernel → proxies.kernel.autoDownload
     · Start → proxies.clash.start · Check nodes (Steam reachability) → proxies.clash.testNodes
     · Proxy nodes → proxies.section.nodes
   · Sign in and fetch → settings.steam.autoFetch · Guided tour → settings.section.tour
   · Import & monitor → crawl.bulk.import
   · Search → alerts.rules.search · Add → alerts.rules.add
     · Price ≤ target / Discount ≥ target / Below historic low → alerts.rules.optionPrice
       / optionPct / optionHistoricLow · Email notifications → alerts.section.smtp
     · Trigger history → alerts.section.history

   两处引号里是**占位符串的起首分句**，不是整串（词典值比标签长，整串塞进句子
   读不通）：· “Add a Clash subscription link” ← proxies.sub.clashPlaceholder
   · “How do I get my Cookie?” ← settings.steam.guideSummary

   `{app}` 的品牌名由组件侧从 `@/appInfo` 的 APP_NAME 传入（同 zh 侧说明）；
   底部按钮复用 common.prev / next / finish。

   `stepRules.p2` 的百分比口径**沿用 alerts 页的既有约定**：中文说「75 折」，
   英文说 “75% off”（见 alerts.rules.hintPct 的中英差异）——两页口径必须一致，
   否则用户在引导里学到的折扣填法和实际页面上的提示对不上。 */

import type { MessageKey } from '../zh-CN'

const productTour: Partial<Record<MessageKey, string>> = {
  /* Opening card (no spotlight target) */
  'productTour.intro.title': 'Welcome to {app}',
  'productTour.intro.p1':
    'A Steam price tracker: it puts every game’s price next to what the other 40+ regions charge, so the cheap region and the discount jump out at you.',
  'productTour.intro.p2':
    'First a quick look at what it looks like, then how to feed it data — just follow Next, and skip any time.',
  'productTour.intro.p3':
    'Leave any time with Skip tour — and reopen it from the {app} logo on the About page.',

  /* Step 1 · Store (sidebar) */
  'productTour.stepStore.title': 'Step 1 · Meet the Store',
  'productTour.stepStore.p1':
    'The item highlighted on the left is the Store (the shop icon) — every price comparison lives on that page: one card per game, with regional prices, discounts and the historic low.',
  'productTour.stepStore.emph':
    'If it looks empty right now, that is expected: it is a display shelf — only games you (or the automatic jobs) fetch show up here.',
  'productTour.stepStore.hint':
    'Learn to read it first, then feed it — the next steps bring the data in.',

  /* Step 2 · Where the data comes from */
  'productTour.stepData.title': 'Step 2 · Why the store is empty, and how data arrives',
  'productTour.stepData.p1':
    'A fresh {app} shows an empty store on purpose: prices are not delivered from the sky — they are crawled from Steam’s regional storefronts, bit by bit.',
  'productTour.stepData.p2': 'Three roads bring the data in, none of which needs any typing:',
  'productTour.stepData.emph':
    '1. Bind your Steam account — wishlist and owned games join automatically. 2. Paste game links on the Tasks page. 3. The top-sellers board refills a batch of popular games every hour. Everything fetched lands here.',
  'productTour.stepData.p3':
    'Prices refresh on their own every 6 hours — set up the next steps once, then let it run.',
  'productTour.stepData.hint':
    'The empty page also shows a matching action button (like “Set up a proxy”) — just follow it.',

  /* Step 3 · Proxies (sidebar) */
  'productTour.stepProxy.title': 'Step 3 · Set up a proxy (needed for auto-fetching)',
  'productTour.stepProxy.emph':
    'Without one, manual imports still work — but the sign-in window may not load and automatic crawls are held back. An empty store is usually this.',
  'productTour.stepProxy.p1':
    'Direct access to the Steam store is mostly blocked, so a proxy is the first brick if you want data to arrive on its own.',
  'productTour.stepProxy.p2':
    'The item highlighted on the left is Proxies (the monitor icon). Next takes you there — no subscription at hand? Skip this step and come back later.',

  /* Step 4 · Clash setup */
  'productTour.stepClash.title': 'Step 4 · Import a Clash subscription',
  'productTour.stepClash.p1':
    '1. Paste your subscription link into the “Add a Clash subscription link” box — the same https:// link you use in your Clash client (kept locally).',
  'productTour.stepClash.p2':
    '2. Click Save subscription; if the kernel is missing, click Install kernel and wait.',
  'productTour.stepClash.p3':
    '3. Click Start to launch the built-in Clash; it finds the nodes that really reach Steam (only those are used).',
  'productTour.stepClash.p4':
    '4. Re-check any time with Check nodes (Steam reachability) — a tick in the Steam column means it works.',
  'productTour.stepClash.hint':
    'No subscription? Proxy nodes lower down takes manual entries and bulk import. You can also skip proxies for now — some features will lack data.',

  /* Step 5 · Me (sidebar) */
  'productTour.stepAccount.title': 'Step 5 · Go to account binding',
  'productTour.stepAccount.p1':
    'With the proxy ready, bind your Steam account. The item highlighted on the left is Me (the person icon) — the binding lives on that page. Next takes you there.',

  /* Step 6 · Steam account binding */
  'productTour.stepBind.title': 'Step 6 · Bind your Steam account',
  'productTour.stepBind.emph':
    'The foundation: binding brings in your wallet balance (top-right pill), wishlist and owned library.',
  'productTour.stepBind.p1':
    '1. Click Sign in and fetch in the highlighted card (the lightning icon).',
  'productTour.stepBind.p2':
    '2. The Steam sign-in window opens (through the proxy you just set up). Enter your account and password, then confirm in the Steam mobile app (Steam Guard).',
  'productTour.stepBind.p3':
    '3. Once verified the window closes and the credentials return automatically — nothing to copy.',
  'productTour.stepBind.hint':
    'In a browser (not the desktop app) there is no auto-fetch button — expand “How do I get my Cookie?” for the 4-step manual guide.',

  /* Step 7 · Tasks (sidebar) */
  'productTour.stepTasks.title': 'Step 7 · Go to the Tasks page',
  'productTour.stepTasks.p1':
    'The item highlighted on the left is Tasks (the loop arrow icon) — crawls and imports live on that page (regions are picked on the Watch pool page). Next takes you there.',

  /* Step 8 · Bulk import */
  'productTour.stepImport.title': 'Step 8 · Add games to watch',
  'productTour.stepImport.p1':
    'The simplest way in — paste the games you want to compare:',
  'productTour.stepImport.p2':
    '1. Open a game on the Steam store (store.steampowered.com) and copy the whole link from the address bar.',
  'productTour.stepImport.p3':
    '2. Paste it into the highlighted bulk import box (many at once, one per line; SteamDB links or bare AppIDs work too).',
  'productTour.stepImport.emph':
    '3. Click Import & monitor — every AppID is detected, priced once and kept in the library as monitoring data (nothing joins the wishlist or the watch pool).',
  'productTour.stepImport.hint':
    'Bound accounts already pull in wishlist and owned games (see the Watch pool page); manual import is monitoring data only — star a game on its card to keep watching it.',

  /* Step 9 · Alerts (sidebar) */
  'productTour.stepAlerts.title': 'Step 9 · Go to the Alerts page',
  'productTour.stepAlerts.p1':
    'The item highlighted on the left is Alerts (the bell icon) — set how low a price must go before you hear about it. Next takes you there.',

  /* Step 10 · Add alert rule */
  'productTour.stepRules.title': 'Step 10 · Set up a price alert',
  'productTour.stepRules.p1':
    '1. Type a game name (Chinese or English) into the highlighted search box, click Search, then pick the game.',
  'productTour.stepRules.p2':
    '2. Pick a region (your billing region by default) and a condition: “Price ≤ target” takes a price you would pay (e.g. 39.9); “Discount ≥ target” takes a percentage (e.g. 75 = 75% off); “Below historic low” takes no number.',
  'productTour.stepRules.emph':
    '3. Click Add and you are done — every price refresh is checked, and a match fires a notification.',
  'productTour.stepRules.p3':
    'Notifications are in-app by default; set up SMTP under Email notifications below to get email as well.',
  'productTour.stepRules.hint':
    'Trigger history on the same page lists everything that has fired.',

  /* Closing card (no spotlight target) */
  'productTour.done.title': 'Done — you know your way around now',
  'productTour.done.emph':
    'The loop you just walked: proxy → account → games → alerts — once set up, it runs itself.',
  'productTour.done.p1':
    'Prices refresh every 6 hours, alerts fire on target, rates sync — nothing to babysit.',
  'productTour.done.p2':
    'Explore as needed: Store for region-by-region prices, Rates for multi-currency trends, Toolbox for CDK bulk activation and more.',
  'productTour.done.p3':
    'To replay it: click the {app} logo on the About page, or open Guided tour on the Me page.',

  /* The overlay's own controls */
  'productTour.action.skip': 'Skip tour',
  'productTour.progress': '{current} / {total}',
}

export default productTour
