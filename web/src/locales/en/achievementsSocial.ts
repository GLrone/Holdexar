/* English dictionary · achievements social (mirrors zh-CN/achievementsSocial.ts, keys must align one to one). */

import type { MessageKey } from '../zh-CN'

const achievementsSocial: Partial<Record<MessageKey, string>> = {
  'achievements.section.social': 'Hunter social',

  'achievements.social.tab.compare': 'Compare',
  'achievements.social.tab.feed': 'Unlocks',
  'achievements.social.tab.notes': 'Notes',

  'achievements.social.compare.title': 'Account compare',
  'achievements.social.compare.selectHint': 'Pick accounts to compare · up to 6',
  'achievements.social.compare.empty': 'No accounts to compare yet',
  'achievements.social.compare.metric.level': 'Hunter level',
  'achievements.social.compare.metric.platinum': 'Platinums',
  'achievements.social.compare.metric.playtime': 'Playtime',
  'achievements.social.compare.metric.challenges': 'Challenges done',
  'achievements.social.compare.metric.unlocked': 'Unlocked',
  'achievements.social.compare.metric.rarity': 'Rarity spread',

  'achievements.social.feed.title': 'Unlock feed',
  'achievements.social.feed.empty': 'No unlocks yet — sync achievements and this lights up',
  'achievements.social.feed.justNow': 'just now',
  'achievements.social.feed.minutesAgo': '{n}m ago',
  'achievements.social.feed.hoursAgo': '{n}h ago',
  'achievements.social.feed.daysAgo': '{n}d ago',

  'achievements.note.title': 'Hunter notes',
  'achievements.note.count': '{n}',
  'achievements.note.placeholder': 'Write something about this achievement…',
  'achievements.note.save': 'Save',
  'achievements.note.delete': 'Delete',
  'achievements.note.confirmDelete': 'Delete this note?',
  'achievements.note.empty': 'No notes yet — open an achievement and leave a line worth keeping',
  'achievements.note.noneMood': 'No notes with this mood yet',
  'achievements.note.maxLen': '{used}/{max}',
  'achievements.note.firstBonus': 'First note +{xp} hunter XP',
  'achievements.note.firstBonusHint': 'The first note on each achievement earns XP',
  'achievements.note.saveFailed': 'Saving failed, try again in a moment',
  'achievements.note.mood.love': 'Loved',
  'achievements.note.mood.wow': 'Wow',
  'achievements.note.mood.hard': 'Brutal',
  'achievements.note.mood.fun': 'Fun',
}

export default achievementsSocial
