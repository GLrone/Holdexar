/* ════════════════════════════════════════════════════════════════════
   English 词典 · productTour（与 zh-CN/productTour.ts 同构，key 必须逐一对齐）。
   文案口径见 zh 侧文件头：只教用户主链（找游戏 → 看价格 → 关注 → 自动更新），
   价格来源与更新周期讲清，账号 = 可选增强（单独一步），代理与任务页不进导览。
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

  /* Step 1 · Find Games (spotlight on the search box) */
  'productTour.stepFind.title': 'Step 1 · Find Games',
  'productTour.stepFind.p1':
    'Type a name in the search box, or use the filters next to it — one card per game.',
  'productTour.stepFind.p2':
    'Each region’s current price, discount and historic low sit right on the card; open a card for the full price history.',
  'productTour.stepFind.emph': 'Find the game first — then decide whether to follow it.',

  /* Step 2 · Where the prices come from (data source + add ≠ follow) */
  'productTour.stepPrice.title': 'Step 2 · Where the prices come from',
  'productTour.stepPrice.p1':
    'The regional prices on each card are fetched automatically by the system from 40+ Steam regions and put side by side on one card.',
  'productTour.stepPrice.p2':
    'Game not listed yet? Paste its store link to add it — the system fetches every region’s price by itself. No Steam account needed.',
  'productTour.stepPrice.emph':
    'Adding ≠ following: adding fetches prices once; followed games refresh automatically every 6 hours.',

  /* Step 3 · Following (continuous updates) */
  'productTour.stepFollow.title': 'Step 3 · Following',
  'productTour.stepFollow.p1':
    'To have the system keep watching a game, follow it here — star its card on Find Games, or add it on this page.',
  'productTour.stepFollow.p2':
    'The follow list always shows each game’s latest price and update time, and you can unfollow any time.',
  'productTour.stepFollow.emph': 'Once you follow a game, the system keeps its prices updated in the background.',

  /* Step 4 · Bind a Steam account (optional enhancement) */
  'productTour.stepBind.title': 'Optional · Bind a Steam account',
  'productTour.stepBind.emph':
    'Everything so far works without binding — this only adds automation.',
  'productTour.stepBind.p1':
    'Once bound, your wishlist, owned games and family library sync in and keep being compared — plus your wallet balance.',
  'productTour.stepBind.p2':
    'Click “Sign in & auto-fetch” and follow the prompts; skip this step any time and come back later.',
  'productTour.stepBind.hint':
    'If you can’t find the auto-fetch button, this card has a manual guide.',

  /* Closing card (no spotlight target) */
  'productTour.done.title': 'Done — from here the system runs itself',
  'productTour.done.emph':
    'Find games → compare prices → follow → automatic updates — the rest is automatic.',
  'productTour.done.p1':
    'Followed games refresh every 6 hours; to get notified when a price hits, set a condition on the Price Alerts page.',
  'productTour.done.p2': 'Everything else can wait — explore it when you need it.',
  'productTour.done.p3':
    'To replay this tour: click the {app} logo on the About page, or find “Guided tour” on the Settings page.',

  /* Overlay controls */
  'productTour.action.skip': 'Skip tour',
  'productTour.progress': '{current} / {total}',
}

export default productTour
