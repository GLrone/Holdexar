/* ════════════════════════════════════════════════════════════════════
   English 词典 · productTour（与 zh-CN/productTour.ts 同构，key 必须逐一对齐）。
   文案口径见 zh 侧文件头：只教用户主链（找游戏 → 看价格 → 关注 → 自动更新 →
   提醒），账号 = 可选增强，代理与任务页不进导览。
   ════════════════════════════════════════════════════════════════════ */

import type { MessageKey } from '../zh-CN'

const productTour: Partial<Record<MessageKey, string>> = {
  /* Intro card (no spotlight target) */
  'productTour.intro.title': 'Welcome to {app}',
  'productTour.intro.p1':
    '{app} keeps an eye on game prices for you: the same game’s prices across 40+ regions, side by side, so the cheapest option is obvious.',
  'productTour.intro.p2':
    'One minute to walk the main path — find games, compare prices, follow the ones you care about; the system keeps them updated and tells you when prices hit.',
  'productTour.intro.p3':
    'Skip tour any time — and reopen it later from the {app} logo on the About page.',

  /* Step 1 · Find Games */
  'productTour.stepFind.title': 'Step 1 · Find Games',
  'productTour.stepFind.p1':
    'This is Find Games: search, browse and filter — one card per game.',
  'productTour.stepFind.p2':
    'Each region’s current price, discount and historic low sit right on the card; open a card for the full price history.',
  'productTour.stepFind.emph': 'Find the game first — then decide whether to follow it.',

  /* Step 2 · Prices update automatically (add ≠ follow) */
  'productTour.stepPrice.title': 'Step 2 · Prices update on their own',
  'productTour.stepPrice.p1':
    'Game not listed yet? Paste its store link to add it — the system fetches every region’s price by itself. No Steam account needed.',
  'productTour.stepPrice.p2':
    'Once prices arrive, the card’s regional prices refresh automatically with every update round.',
  'productTour.stepPrice.emph':
    'Adding ≠ following: adding fetches prices once; whether to keep watching is your call in the next step.',

  /* Step 3 · Following (continuous updates) */
  'productTour.stepFollow.title': 'Step 3 · Following',
  'productTour.stepFollow.p1':
    'To have the system keep watching a game, follow it here — star its card on Find Games, or add it on this page.',
  'productTour.stepFollow.p2':
    'The follow list always shows each game’s latest price and update time, and you can unfollow any time.',
  'productTour.stepFollow.emph': 'Once you follow a game, the system keeps its prices updated in the background.',

  /* Step 4 · Price Alerts (notify on condition) */
  'productTour.stepAlert.title': 'Step 4 · Price Alerts',
  'productTour.stepAlert.p1':
    'Set a condition — say “Price ≤ target” with a price you would happily pay; pause or re-enable any alert at any time.',
  'productTour.stepAlert.p2':
    'Click “Add” and you are set — every update is checked and you get notified the moment a rule matches; the trigger history keeps a record.',
  'productTour.stepAlert.emph':
    'Following means the system keeps watching for you; alerts mean it actively notifies you when a condition is met — the two are independent.',

  /* Step 5 · Game Library (parallel capability) */
  'productTour.stepGamelib.title': 'Step 5 · Game Library',
  'productTour.stepGamelib.p1':
    'This is where your owned and linked games live — library analysis, family library and playtime live in this group of pages.',
  'productTour.stepGamelib.p2':
    'Bind a Steam account and your wishlist, owned games and family library sync in automatically — everything about prices works without it.',

  /* Closing card (no spotlight target) */
  'productTour.done.title': 'Done — from here the system runs itself',
  'productTour.done.emph':
    'Find games → compare prices → follow → automatic updates → alerts when prices hit — the rest is automatic.',
  'productTour.done.p1':
    'For extra automation you can bind a Steam account on the Settings page (syncs wishlist, owned games and family library) — entirely optional.',
  'productTour.done.p2': 'Everything else can wait — explore it when you need it.',
  'productTour.done.p3':
    'To replay this tour: click the {app} logo on the About page, or find “Guided tour” on the Settings page.',

  /* Overlay controls */
  'productTour.action.skip': 'Skip tour',
  'productTour.progress': '{current} / {total}',
}

export default productTour
