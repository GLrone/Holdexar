/* English 词典 · family（与 zh-CN/family.ts 同构，key 必须逐一对齐）。
   对应源文件：views/family/Index.vue。

   术语锁定分 A / B 两组，逐字照抄，不要另起译法：
   · Family / family library / shared library（后者是另一个集合）
   · contribution split / acquisition heatmap / value insights
   · Member insights / Purchase activity / Play activity / Exclusive
   · `family.member.recent30` 的数据口径是**入库**不是游玩，故用 "Acquired in
     last 30 days"，**不用 active**（D6）。
   · `family.role.family`（'Family'）与 `family.role.familyMember`
     （'family member'）是刻意的长短分工，别统一成一条。 */

import type { MessageKey } from '../zh-CN'

const family: Partial<Record<MessageKey, string>> = {
  /* Module head (shared by the data-section anchor and the title) */
  'family.section.module': 'Steam family library',
  'family.module.sub': 'Up to 6 members · Contribution / Heatmap / Purchases / Wishlist',

  /* Tabs */
  'family.tab.contrib': 'Contribution split',
  'family.tab.heat': 'Acquisition heatmap',
  'family.tab.value': 'Value insights',
  'family.tab.insights': 'Member insights',
  'family.tab.buy': 'Purchase activity',
  'family.tab.play': 'Play activity',
  'family.tab.wish': 'Family wishlist',
  'family.tab.lib': 'Family library',

  /* Roles (D8: short 'Family' for the badge, 'family member' in prose) */
  'family.role.primary': 'Primary',
  'family.role.family': 'Family',
  'family.role.familyMember': 'family member',

  /* Member row */
  'family.member.unnamed': 'Member {id}',
  'family.member.friendCode': 'Steam friend code · {code}',
  'family.member.notLinked': 'Not linked',
  'family.member.owned': 'Owned {n}',
  'family.member.exclusive': 'Exclusive {n}',
  'family.member.recent30': 'Acquired in last 30 days {n}',
  'family.member.remove': 'Remove',

  /* Region popover */
  'family.regionPop.who': '{role} · currently {region}',
  'family.regionPop.search': 'Search regions (name / code)…',
  'family.regionPop.empty': 'No matching regions',

  /* Sync family group */
  'family.action.syncing': 'Syncing…',
  'family.action.sync': '⟳ Sync family group',
  'family.sync.success': 'Family “{name}” synced — {n} members now tracked',
  'family.sync.notJoined': 'This account has not joined a family group',
  'family.group.unnamed': 'Unnamed',

  /* Add member */
  'family.add.placeholder': 'Add member: Steam friend code (e.g. 998167239) or SteamID64 — resolved as you type',
  'family.add.needInput': 'Enter a friend code or SteamID64 first — the account must resolve before it can be added',
  'family.add.trackFailed': 'Added to the list, but tracking failed: {err}',
  'family.add.success': 'Added {name} — now tracked in the watch pool. Link a cookie and run “Sync family group” to fill in the rest.',
  'family.add.confirming': 'Adding…',
  'family.add.action': 'Add',
  'family.add.invite': '＋ Invite a member to the family group ({n} / 6 — room for {left} more, drag to reorder)',

  /* Friend-code resolve preview */
  'family.resolve.loading': 'Resolving account…',
  'family.resolve.noName': '(no nickname — profile may be private)',
  'family.resolve.ok': 'Resolved · press Enter or click Add',
}

export default family
