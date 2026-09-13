/* English 词典 · famPlay（与 zh-CN/famPlay.ts 同构，key 必须逐一对齐）。
   对应源文件：views/family/tabs/FamPlay.vue。

   措辞对齐（这些词的译法在别处已定，本页沿用）：
   · Total playtime —— D2（与 famLib.kpi.playtime 同一个数，逐字一致）；
   · X per member —— D3，`famPlay.kpi.avgLabel` 补全被平均的量词
     （"Playtime per member (2 weeks)"，不是 "avg 2-week"）；
   · playtime / Last played / Member —— B 表与 A 表；
   · Play activity —— B 表「游玩动态」，刷新按钮据此写成 Refresh play activity。

   时长单位（`h` / `kh`）不在这里：它不是语言中立的，跨模块共用，
   落在 common.hours（`{h} hrs`）与 common.hoursK（`{h}k hrs`）。 */

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
