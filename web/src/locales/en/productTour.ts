/* English 词典 · productTour（与 zh-CN/productTour.ts 同构，key 必须逐一对齐）。
   对应源文件：components/ProductTour.vue。

   这一份是**重写**而非直译：中文原文是口语化的引导语气，逐字对译会变成说明书，
   而且英文比中文长 1.5–2 倍，直译会撑破 440px 宽的气泡。英文按英语产品的引导
   文案写：第二人称、动词开头、一句一件事，长度与中文相当。

   点名界面标签时的取词出处（与各页词典逐字一致，改一处要两处同步）：
   · Store / Proxies / Me → shell.ts 的 nav.*
   · Import & monitor → crawl.bulk.import
   · Price ≤ target / Discount ≥ target / Below historic low → alerts.rules.optionPrice
     / optionPct / optionHistoricLow
   · Sign in and fetch → settings.steam.autoFetch · Guided tour → settings.section.tour

   `{app}` 的品牌名由组件侧从 `@/appInfo` 的 APP_NAME 传入（同 zh 侧说明）；
   底部按钮复用 common.prev / next / finish。

   `stepRules.p1` 的百分比口径**沿用 alerts 页的既有约定**：中文说「75 折」，
   英文说 “75% off”（见 alerts.rules.hintPct 的中英差异）——两页口径必须一致，
   否则用户在引导里学到的折扣填法和实际页面上的提示对不上。 */

import type { MessageKey } from '../zh-CN'

const productTour: Partial<Record<MessageKey, string>> = {
  /* Opening card (no spotlight target) */
  'productTour.intro.title': 'Welcome to {app}',
  'productTour.intro.p1':
    'A Steam multi-region price tracker: one game, 40+ regional prices side by side — the cheap region jumps out at a glance.',
  'productTour.intro.p2':
    'One minute to walk the main path — add games, watch prices, set an alert. That is all it takes.',
  'productTour.intro.p3':
    'Skip tour any time — and reopen it later from the {app} logo on the About page.',

  /* Step 1 · Store (what the comparisons look like) */
  'productTour.stepData.title': 'Step 1 · All the comparisons live here',
  'productTour.stepData.p1':
    'This is the Store: one card per game, with each region’s current price, discount and historic low.',
  'productTour.stepData.p2':
    'Empty right now? That is normal — prices are fetched from Steam automatically. Tell it which games you care about, and the data will come in.',
  'productTour.stepData.emph':
    'Nothing needs to be configured first — the next step shows how to add games.',
  'productTour.stepData.hint':
    'Come back here after adding games and the cards will appear on their own.',

  /* Step 2 · Import games (the main action) */
  'productTour.stepImport.title': 'Step 2 · Add the games you want to compare',
  'productTour.stepImport.p1':
    'Open a game on the Steam store and copy the link from the address bar.',
  'productTour.stepImport.p2':
    'Paste it into this box (several at once, one per line) and click “Import & monitor”.',
  'productTour.stepImport.emph':
    'Done. Prices refresh automatically every 6 hours — nothing to babysit.',
  'productTour.stepImport.hint':
    'Back on the Store page the cards appear once the first refresh finishes — star any card to keep watching that game.',

  /* Step 3 · Price alerts (the main action) */
  'productTour.stepRules.title': 'Step 3 · Get told when a price drops',
  'productTour.stepRules.p1':
    'Search and pick a game, then choose a condition — say “Price ≤ target” with a price you would happily pay, or “Discount ≥ target”, or “Below historic low”.',
  'productTour.stepRules.p2':
    'Click “Add” and you are set — every refresh is checked, and you get a notification the moment a rule matches.',
  'productTour.stepRules.emph':
    'Alerts arrive in the app by default — nothing extra to set up.',

  /* Optional branch · proxy (a fix for a struggling network) */
  'productTour.stepProxy.title': 'Optional · A proxy, only if the network fights you',
  'productTour.stepProxy.emph':
    'Feel free to skip this entirely — fetching works over a direct connection, and none of the three steps above depends on it.',
  'productTour.stepProxy.p1':
    'It is only worth setting up a proxy on the Proxies page if prices never arrive, or the sign-in window will not open.',
  'productTour.stepProxy.p2':
    'It accepts a subscription link and walks you through it — the details can wait until you actually need them.',

  /* Optional branch · Steam account binding (automation booster) */
  'productTour.stepBind.title': 'Optional · Bind your Steam account',
  'productTour.stepBind.emph':
    'Everything above works without binding — this only adds automation.',
  'productTour.stepBind.p1':
    'Once bound, your wishlist and owned games join automatically and stay tracked — plus your wallet balance shows up.',
  'productTour.stepBind.p2':
    'Click “Sign in and fetch” and follow the prompts; if the sign-in window will not open, set up the proxy from the previous step first.',
  'productTour.stepBind.hint':
    'If you do not see an auto-fetch button, the same card holds a manual guide.',

  /* Closing card (no spotlight target) */
  'productTour.done.title': 'Done — the main path is just three steps',
  'productTour.done.emph':
    'Add games → watch prices → set an alert — from here the system runs itself.',
  'productTour.done.p1':
    'Prices refresh every 6 hours, alerts fire on their own — nothing to babysit.',
  'productTour.done.p2':
    'The rest (rates, toolbox, family sharing…) can wait — explore when you need it.',
  'productTour.done.p3':
    'To replay this tour: the {app} logo on the About page, or Guided tour on the Me page.',

  /* The overlay's own controls */
  'productTour.action.skip': 'Skip tour',
  'productTour.progress': '{current} / {total}',
}

export default productTour
