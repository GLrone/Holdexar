/* English 词典 · famWish（与 zh-CN/famWish.ts 同构，key 必须逐一对齐）。
   对应源文件：views/family/tabs/FamWish.vue。

   ⚠️ famWish.band.* 与 famValue.band.* 是同一套价格分档。中文两侧写法不同
   （此处 ≥¥200、FamValue 写 ¥200+，且那边多一档「未定价」），**英文必须逐字一致**，
   故 band.gte200 两侧都写 '¥200+'——不受各自中文写法影响。

   术语：「折扣中」= On sale（同 trendDrawer.price.discount）、
   「即将推出」= Coming soon、「家庭库」= family library（小写）、
   「未收录」= Not listed（同 gameCard.cdk.notListed）、「成员」= members。 */

import type { MessageKey } from '../zh-CN'

const famWish: Partial<Record<MessageKey, string>> = {
  /* Category labels (filter chips + KPI labels + card tags share these) */
  'famWish.tag.familyOwned': 'In family library',
  'famWish.tag.multiWant': 'Wanted by 2+',
  'famWish.tag.onSale': 'On sale',
  'famWish.tag.comingSoon': 'Coming soon',

  /* KPI row (7 cards) */
  'famWish.kpi.total': 'Wishlist total',
  'famWish.kpi.totalSub': 'Distinct titles',
  'famWish.kpi.hitRate': '{pct}% hit rate',
  'famWish.kpi.multiWantSub': 'Wanted by 2+ members',
  'famWish.kpi.maxDiscount': 'Up to -{pct}%',
  'famWish.kpi.noDiscount': 'No discounts',
  'famWish.kpi.comingSoonSub': 'Unreleased titles',
  'famWish.kpi.value': 'Total value',
  'famWish.kpi.avgPrice': 'Avg ¥{amt}',
  'famWish.kpi.members': 'Members',
  'famWish.kpi.membersFallback': 'Family not synced (all members tracked)',
  'famWish.kpi.membersSub': 'Family members',

  /* Empty / loading states */
  'famWish.empty.loading': 'Aggregating family wishlist…',
  'famWish.empty.noData':
    'No wishlist data yet — it appears once family members sync their wishlists (you can sync manually on the Watch pool page)',
  'famWish.empty.noMemberData': 'No member data',
  'famWish.empty.noGenreData': 'No genre data',
  'famWish.empty.noMatch': 'No matching titles',

  /* Toolbar */
  'famWish.toolbar.updatedAt': 'Updated {time}',
  'famWish.toolbar.refresh': 'Refresh wishlist',
  'famWish.search.placeholder': 'Search by title or AppID…',

  /* Three charts in the left column */
  'famWish.chart.memberDist': 'Wishlist distribution by member',
  'famWish.chart.topTags': 'Top 8 genres',
  'famWish.chart.priceBands': 'Price distribution',
  'famWish.donut.total': 'Total',

  /* Price bands (donut legend; must match famValue.band.* word for word) */
  'famWish.band.free': 'Free',
  'famWish.band.lt50': '<¥50',
  'famWish.band.from50to100': '¥50-100',
  'famWish.band.from100to200': '¥100-200',
  'famWish.band.gte200': '¥200+',

  /* Price text and card tags */
  'famWish.price.notListed': 'Not listed',
  'famWish.tag.wantCount': 'Wanted by {n}',

  /* Pager line (inline <b> lives in the value, rendered with v-html; the accent
     colour lives in CSS — `.wl-pager :deep(b)`) */
  'famWish.pager.total': '<b>{n}</b> titles',
  'famWish.pager.capped':
    '<b>{n}</b> titles · showing first 60',
}

export default famWish
