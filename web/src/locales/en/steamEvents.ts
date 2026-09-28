/* Steam event calendar (views/events/Index.vue + SteamEventCountdown card).
   Event names themselves are data (backend nameEn/nameZh); this module only
   carries UI semantics. */

const steamEvents = {
  'steamEvents.updatedAt': 'Synced at {time}',
  'steamEvents.refreshing': 'Data is stale, re-syncing',
  'steamEvents.loadFailed': 'Calendar is temporarily unavailable, will retry automatically',
  'steamEvents.empty': 'No announced events yet',
  'steamEvents.retry': 'Retry',
  'steamEvents.goCalendar': 'View full calendar →',
  'steamEvents.noNext': 'No upcoming events announced yet',

  'steamEvents.section.hero': 'Event calendar',
  'steamEvents.section.live': 'Live now',
  'steamEvents.section.upcoming': 'Upcoming',
  'steamEvents.section.ended': 'Recently ended',

  'steamEvents.countdown.label': 'Time until {name} starts',
  'steamEvents.countdown.days': 'd',
  'steamEvents.countdown.hours': 'h',
  'steamEvents.countdown.minutes': 'm',
  'steamEvents.countdown.seconds': 's',

  'steamEvents.startsToday': 'Starts today',
  'steamEvents.startsTomorrow': 'Starts tomorrow',
  'steamEvents.startsIn': 'Starts in {n} days',
  'steamEvents.endsToday': 'Ends today',
  'steamEvents.endsIn': '{n} days left',
  'steamEvents.dateRange': '{start} – {end}',
  'steamEvents.tzNote': '(PT)',

  'steamEvents.category.seasonal_sale': 'Seasonal Sale',
  'steamEvents.category.next_fest': 'Next Fest',
  'steamEvents.category.themed_fest': 'Themed Fest',
} as const

export default steamEvents
