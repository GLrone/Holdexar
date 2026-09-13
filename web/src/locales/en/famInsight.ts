/* English 词典 · famInsight（与 zh-CN/famInsight.ts 同构，key 必须逐一对齐）。
   对应源文件：views/family/tabs/FamInsight.vue。

   措辞对齐（这些词的译法在别处已定，本页沿用）：
   · Active / Warm / Cold / Dormant —— B 表与 D7（`status.*` 四条的状态徽章）；
   · acquired / acquisition —— B 表「入库」；
   · monthly average —— B 表「月均」；tier —— B 表「档位·分层」；
   · Health score —— U2（不写成 Activity score）；
   · member / members —— A 表与 D9（不用 Contributors）；
   · family library —— A 表「家庭库」；family health score 与
     `famLib.kpi.*` 的 "Family library ..." 区分得开。

   `status.*` 与 `tier.*` 是两组不同的东西，别合并：前者是成员行的短徽章
   （译文由 store 回 key、组件 t() 现取），后者是分档统计格、带阈值说明。 */

import type { MessageKey } from '../zh-CN'

const famInsight: Partial<Record<MessageKey, string>> = {
  /* Empty states */
  'famInsight.empty.loading': 'Loading family library…',
  'famInsight.empty.noData':
    'No acquisition dates yet — member activity tiers rely on rt_time_acquired',

  /* Status badges (fed by the store's memberActivity[].statusKey) */
  'famInsight.status.active': 'Active',
  'famInsight.status.warm': 'Warm',
  'famInsight.status.cold': 'Cold',
  'famInsight.status.dormant': 'Dormant',

  /* Tier counts (fed by this page's STATUS_META labelKey) */
  'famInsight.tier.active': 'Active <14d',
  'famInsight.tier.warm': 'Warm <60d',
  'famInsight.tier.cold': 'Cold <180d',
  'famInsight.tier.dormant': 'Dormant/never',

  /* Member activity list */
  'famInsight.activity.title': 'Member acquisition activity',
  'famInsight.memberFallback': 'Member {id}',
  'famInsight.member.meta': '<b>{total}</b> titles · monthly average <b>{avg}</b>',
  'famInsight.member.last': 'Last acquired {n}d ago',
  'famInsight.member.never': 'Never acquired',

  /* Health score and tier counts */
  'famInsight.health.title': 'Family health score',
  'famInsight.health.ring': 'Health score',
  'famInsight.tier.title': 'Activity tier counts',
  'famInsight.note':
    'How to read: days since the member’s last acquisition (rt_time_acquired); health score = the average of the four tiers (100/75/50/25).',
}

export default famInsight
