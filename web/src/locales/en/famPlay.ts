/* English 词典 · famPlay —— views/gamelib/tabs/GlPlay.vue（与 zh-CN/famPlay.ts 同构，key 必须逐一对齐）。
   措辞沿用既有译法：Total playtime（与 famLib.kpi.playtime 同一个数、逐字一致）、
   "Playtime per member (2 weeks)"（补全被平均的量词，不是 avg）、playtime / Last played / Member、
   Play activity（刷新按钮据此写 Refresh play activity）。
   不变量：时长单位 h / kh 落 common.hours / common.hoursK，不在本模块。 */

import type { MessageKey } from '../zh-CN'

const famPlay: Partial<Record<MessageKey, string>> = {
  /* Empty states */
  'famPlay.empty.loading': 'Loading family library…',
  'famPlay.empty.noPlay': 'No member playtime yet — sync your family group to see it',
  'famPlay.empty.noRecords': 'No playtime recorded',

  /* KPI cards */
  'famPlay.kpi.totalLabel': 'Total playtime',
  'famPlay.kpi.totalSub': 'All members combined',
  'famPlay.kpi.recentLabel': 'Playtime (2 weeks)',
  'famPlay.kpi.recentSub': 'All members combined',
  'famPlay.kpi.avgLabel': 'Playtime per member (2 weeks)',
  'famPlay.kpi.members': '{n} members',
  'famPlay.kpi.topLabel': 'Most active (2 weeks)',
  'famPlay.kpi.topShare': '{h} · {pct}% of total',

  /* Source and refresh */
  'famPlay.source':
    'Source: Steam Web API (GetOwnedGames playtime) · Client sessions appear after the next sync',
  'famPlay.action.refresh': 'Refresh play activity',

  /* Member cards */
  'famPlay.memberFallback': 'Member {id}',
  'famPlay.member.sub': 'Owns <b>{owned}</b> titles · played <b>{recent}</b> in 2 weeks',
  'famPlay.tag.mostActive': '🔥 Most active',
  'famPlay.tag.noPlay2w': 'No playtime in 2 weeks',

  /* Bar row labels */
  'famPlay.bar.recent': '2 weeks',
  'famPlay.bar.total': 'Total',
}

export default famPlay
