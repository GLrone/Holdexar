/* English dictionary · Trophy hall (mirrors zh-CN/achievementsHall.ts, keys must align one to one). */

import type { MessageKey } from '../zh-CN'

const achievementsHall: Partial<Record<MessageKey, string>> = {
  /* ── Section anchor (data-section) ── */
  'achievements.section.challenges': 'Hunter challenges',

  /* ── Level tiers (legend reuses the platinum trophy art) ── */
  'achievements.level.tier.bronze': 'Bronze hunter',
  'achievements.level.tier.silver': 'Silver hunter',
  'achievements.level.tier.gold': 'Gold hunter',
  'achievements.level.tier.platinum': 'Platinum hunter',
  'achievements.level.tier.legend': 'Legendary hunter',

  /* ── Level card ── */
  'achievements.level.card.title': 'Hunter level',
  'achievements.level.card.totalXp': 'Lifetime hunter XP',
  'achievements.level.card.progressLabel': 'Level progress',
  'achievements.level.card.nextLevel': '{xp} XP to reach level {n}',
  'achievements.level.card.lastCost': 'Previous level cost',
  'achievements.level.card.nextCost': 'Next level needs',
  'achievements.level.card.costGrowth': '+{pct}%',
  'achievements.level.card.costTitle': 'Level-up cost',
  'achievements.level.card.curveTitle': 'Upcoming level costs',
  'achievements.level.card.curveBar': 'Level {level} · {xp} XP',
  'achievements.level.card.milestonesTitle': 'Milestone nodes',
  'achievements.level.card.milestoneNext': 'Next milestone',
  'achievements.level.card.recentTitle': 'Recent XP',
  'achievements.level.card.recentEmpty': 'No XP records yet',
  'achievements.level.card.xpGain': '+{xp}',

  /* ── XP bonuses ── */
  'achievements.level.bonus.streak': 'Streak: {days} days · XP +{percent}%',

  /* ── XP sources (recent list rows) ── */
  'achievements.level.event.achievementUnlock': 'Achievement unlocked',
  'achievements.level.event.platinum': 'Platinum earned',
  'achievements.level.event.playtimeBucket': 'Deep session',
  'achievements.level.event.streakDay': 'Streak day',
  'achievements.level.event.challengeClaim': 'Challenge reward',
  'achievements.level.event.comment': 'Achievement note',
  'achievements.level.event.baseline': 'Career carryover',

  /* ── Challenge center ── */
  'achievements.challenge.tier.bronze': 'Bronze',
  'achievements.challenge.tier.silver': 'Silver',
  'achievements.challenge.tier.gold': 'Gold',
  'achievements.challenge.category.milestone': 'Milestone',
  'achievements.challenge.category.cumulative': 'Cumulative',
  'achievements.challenge.category.streak': 'Streak',
  'achievements.challenge.category.collection': 'Collection',
  'achievements.challenge.category.platinum': 'Platinum',
  'achievements.challenge.category.comment': 'Notes',
  'achievements.challenge.category.social': 'Together',
  'achievements.challenge.status.active': 'In progress',
  'achievements.challenge.status.completed': 'Ready to claim',
  'achievements.challenge.status.claimed': 'Claimed',
  'achievements.challenge.action.claim': 'Claim reward',
  'achievements.challenge.action.reward': '+{n} XP',
  'achievements.challenge.action.granted': '+{n} XP added',
  'achievements.challenge.action.failed': 'Claim failed: {error}',
  'achievements.challenge.action.leveledUp': 'Hunter level up: level {n}!',
  'achievements.challenge.progressOf': '{progress}/{target}',
  'achievements.challenge.summary': '{done}/{total} completed · {claimable} ready to claim',
  'achievements.challenge.filter.all': 'All',

  /* ── Challenge items ── */
  'achievements.challenge.item.first_trophy.name': 'First catch',
  'achievements.challenge.item.first_trophy.desc': 'Unlock your first achievement',
  'achievements.challenge.item.hunter_twelve.name': 'Twelve catches',
  'achievements.challenge.item.hunter_twelve.desc': 'Unlock 12 achievements in total',
  'achievements.challenge.item.hunter_fifty.name': 'Half-century hunter',
  'achievements.challenge.item.hunter_fifty.desc': 'Unlock 50 achievements in total',
  'achievements.challenge.item.hunter_hundred.name': 'Hall of a hundred',
  'achievements.challenge.item.hunter_hundred.desc': 'Unlock 100 achievements in total',
  'achievements.challenge.item.streak_three.name': 'Three-day streak',
  'achievements.challenge.item.streak_three.desc': 'Earn achievements 3 days in a row',
  'achievements.challenge.item.streak_seven.name': 'Seven-day streak',
  'achievements.challenge.item.streak_seven.desc': 'Earn achievements 7 days in a row',
  'achievements.challenge.item.streak_thirty.name': 'Month-long hunt',
  'achievements.challenge.item.streak_thirty.desc': 'Earn achievements 30 days in a row',
  'achievements.challenge.item.platinum_first.name': 'First platinum',
  'achievements.challenge.item.platinum_first.desc': 'Fully complete your first game',
  'achievements.challenge.item.platinum_five.name': 'Platinum shelf',
  'achievements.challenge.item.platinum_five.desc': 'Fully complete 5 games',
  'achievements.challenge.item.platinum_twelve.name': 'Platinum hall',
  'achievements.challenge.item.platinum_twelve.desc': 'Fully complete 12 games',
  'achievements.challenge.item.breadth_twenty.name': 'A growing park',
  'achievements.challenge.item.breadth_twenty.desc': 'Earn achievements in 20 games',
  'achievements.challenge.item.full_clear_first.name': 'Full sweep',
  'achievements.challenge.item.full_clear_first.desc': 'Complete every achievement in one game',
  'achievements.challenge.item.full_clear_triple.name': 'Triple full house',
  'achievements.challenge.item.full_clear_triple.desc': 'Complete every achievement in 3 games',
  'achievements.challenge.item.hours_hundred.name': 'Hundred-hour hunt',
  'achievements.challenge.item.hours_hundred.desc': 'Accumulate 100 hours of playtime',
  'achievements.challenge.item.hours_thousand.name': 'Thousand-hour field',
  'achievements.challenge.item.hours_thousand.desc': 'Accumulate 1,000 hours of playtime',
  'achievements.challenge.item.buckets_ten.name': 'Deep patrol ×10',
  'achievements.challenge.item.buckets_ten.desc': 'Push games into deeper playtime tiers 10 times in total',
  'achievements.challenge.item.note_first.name': 'First note',
  'achievements.challenge.item.note_first.desc': 'Write your first note on an achievement',
  'achievements.challenge.item.note_gallery.name': 'A book of notes',
  'achievements.challenge.item.note_gallery.desc': 'Write 25 achievement notes in total',
  'achievements.challenge.item.social_duo.name': 'Better together',
  'achievements.challenge.item.social_duo.desc': 'Add a family member so another account has achievements too',
  'achievements.challenge.item.social_trio.name': 'Squad of hunters',
  'achievements.challenge.item.social_trio.desc': 'Add family members until 3 accounts each hold 10+ achievements',

  /* ── Fact notices (dynamic island; fixed key names, referenced by stores/factNotices.ts) ── */
  'achievements.fact.levelUp': 'Hunter level up: {fromLevel} → {toLevel}',
  'achievements.fact.challengeDone': 'Challenge complete: {name} — reward ready to claim',
  'achievements.fact.unlockedBatch': '{count} new achievements: {titles}',
}

export default achievementsHall
